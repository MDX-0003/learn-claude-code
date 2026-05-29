# Doc E：多Agent协作 —— 子Agent、队友、通信与任务系统

## 读者定位

读者已理解单个 Agent Loop 的全部机制。现在需要理解"Agent 如何把工作分派给子 Agent、如何与自主队友协作、任务如何管理、工作区如何隔离"。

---

## 一、两层协作架构

s20 支持两层 Agent 协作：

```
Lead Agent（主循环，agent_loop）
  │
  ├─ task/subagent ─────→ 同步子 Agent（有独立的循环，30 轮上限，返回摘要）
  │                         不能调用 spawn_teammate（防止无限递归）
  │
  └─ spawn_teammate ───→ 自主队友线程（有独立的线程 + 循环）
                           可以 idle poll → auto-claim 任务 → 继续工作
                           通过 MessageBus 与 Lead 通信
```

核心区别：
- **子 Agent**：同步调用，一次性任务，用完即弃
- **队友**：异步运行，持续存在，自主认领任务

---

## 二、子Agent：spawn_subagent()

`s20_comprehensive/code.py:1012-1044`

**要解决的问题**：Lead Agent 需要把某个独立任务（如"搜索所有 TODO 注释"）外包出去，只关心最终结果，不关心中间过程。

### 2.1 完整流程

```python
def spawn_subagent(description: str) -> str:
    messages = [{"role": "user", "content": description}]
    for _ in range(30):                                          # 最多 30 轮
        trigger_hooks("PreModelCall", messages,
                      {"system_prompt": SUB_SYSTEM}, SUB_TOOLS)  # 钩子仍然触发
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM, messages=messages,
            tools=SUB_TOOLS, max_tokens=8000)
        trigger_hooks("PostModelCall", response, messages)

        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            break                                                # 无工具调用 → 完成

        # 工具执行循环（和 agent_loop 相同的 PreToolUse/PostToolUse 钩子）
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = trigger_hooks("PreToolUse", block)         # permission 仍然生效
            if blocked:
                output = str(blocked)
            else:
                handler = SUB_HANDLERS.get(block.name)
                output = call_tool_handler(handler, block.input, block.name)
                trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result", ...})
        messages.append({"role": "user", "content": results})

    # 从最后一条 assistant 消息提取文本作为摘要返回
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = extract_text(msg["content"])
            if text:
                return text
    return "Subagent finished without a text summary."
```

### 2.2 设计要点

**SUB_SYSTEM**（L953-957）：子 Agent 有独立的 system prompt，不包含 Lead 的 tools 列表，只告诉它"完成你的任务，返回摘要"。

**SUB_TOOLS**（L960-993）：只有 5 个基础文件工具（bash/read/write/edit/glob），没有 spawn_teammate、task、cron 等高级工具。这防止了无限递归。

**钩子复用**：子 Agent 内部仍然触发 `PreToolUse`/`PostToolUse` 钩子。这意味着 `permission_hook` 在子 Agent 的工具调用上也有效 —— 子 Agent 不能绕过安全检查。

**阻塞模式**：`spawn_subagent()` 是同步函数。调用它的 Lead Agent 会阻塞等待，直到子 Agent 返回。这是通过 `task` 工具在 `agent_loop` 的工具循环中同步调用 `handler(**block.input)` 实现的。

---

## 三、队友：spawn_teammate_thread()

`s20_comprehensive/code.py:606-816`

**要解决的问题**：有些工作需要持续进行 —— 比如一个队友负责监控代码质量，另一个负责处理 incoming 任务。它们应该独立运行，通过消息通信。

### 3.1 队友线程的完整生命周期

```
Lead 调用 spawn_teammate(name, role, prompt)
  │
  ├─ 创建 Thread(target=run, daemon=True)
  │
  └─ 线程内部：
       │
       ├─ [WORK PHASE] 最多 50 轮 LLM 调用
       │   ├─ 检查 inbox（处理 shutdown/plan_approval）
       │   ├─ 调 LLM → 执行工具 → 循环
       │   ├─ 遇到 submit_plan → 暂停，等待 Lead 审批
       │   └─ 遇到 idle → 进入 IDLE PHASE
       │
       ├─ [IDLE PHASE] 最多 IDLE_TIMEOUT 秒（60s）
       │   ├─ 每秒 poll inbox（shutdown 消息优先）
       │   ├─ poll 未认领的任务（auto-claim）
       │   └─ 有消息/任务 → 回到 WORK PHASE
       │
       └─ [SHUTDOWN] timeout 或收到 shutdown_request
           └─ 发送 summary 给 Lead → 退出线程
```

### 3.2 关键设计：idle_poll 与 auto-claim

```python
IDLE_POLL_INTERVAL = 5    # 每 5 秒检查一次
IDLE_TIMEOUT = 60          # 60 秒无活动则退出

def idle_poll(agent_name, messages, name, role, worktree_context):
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):  # 最多 12 次
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            # 优先处理 shutdown_request
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    ...
                    return "shutdown"
            messages.append({"role": "user", "content": "<inbox>...</inbox>"})
            return "work"   # 有新消息 → 回到工作阶段

        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: "
                               f"{task_data['subject']}</auto-claimed>"})
                return "work"   # 认领到任务 → 回到工作阶段

    return "timeout"  # 60 秒无活动 → 关闭
```

**优先级**：inbox 消息 > 未认领任务 > timeout。即使有未认领任务，如果有 inbox 消息也会先处理消息。

### 3.3 工具限制

队友只有基础文件工具 + `send_message` + `submit_plan` + `list_tasks` + `claim_task` + `complete_task`（L676-716）。没有 `task`（不能再派生子 Agent）和 `spawn_teammate`（不能再创建队友）。

### 3.4 submit_plan 审批门

队友在执行过程中遇到需要 Lead 批准的决策时，可以调用 `submit_plan`。这触发一个协议：队友暂停（`protocol_ctx["waiting_plan"]` 被设置），轮询 inbox 等待 `plan_approval_response`。Lead 通过 `review_plan` 工具批准或拒绝。

---

## 四、MessageBus：基于文件的通信

`s20_comprehensive/code.py:472-502`

**要解决的问题**：Agent 和队友运行在不同线程中，需要一个通信通道。不能用内存 queue（队友线程可能已经终止），不能太复杂（这是教学项目）。

**怎么做**：JSONL 文件作为邮箱。每个 Agent 有一个 inbox 文件（`.mailboxes/{name}.jsonl`），发送 = 追加一行 JSON，接收 = 读取全部行后删除文件。

```python
class MessageBus:
    def send(self, from_agent, to_agent, content, msg_type="message", metadata=None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()    # 读后删除
        return msgs
```

消息类型（L68-69）：

```python
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}
```

### 4.1 Lead 的消息处理

Lead 在每轮 REPL 循环结束时检查 inbox（L2081-2092）。非 protocol 消息作为 `[Inbox]` 注入到下一轮对话。Protocol 消息（shutdown_response、plan_approval_response）在 `consume_lead_inbox()` 中通过 `match_response()` 路由到 `pending_requests` 字典。

### 4.2 写入安全性

多个线程可能同时向同一个 inbox 文件追加。POSIX 保证小于 `PIPE_BUF` 字节的 `write()` 是原子的。对于 JSONL 格式的简单消息，这在实践中是安全的。

---

## 五、协议：Shutdown 与 Plan Approval

`s20_comprehensive/code.py:505-547, 833-859`

两种结构化协议都遵循请求-响应模式，通过 `request_id` 匹配：

### Shutdown

```
Lead: request_shutdown("builder")
  → BUS.send("lead","builder","Shut down.","shutdown_request",{request_id})
  → Lead 不等待

Builder 线程: inbox poll 发现 shutdown_request
  → BUS.send("builder","lead","Shutting down.","shutdown_response",{request_id, approve:True})
  → 退出线程

Lead: check_inbox → consume_lead_inbox → match_response → pending_requests[req_id].status="approved"
```

### Plan Approval

```
Builder 线程: submit_plan("I plan to refactor X by doing Y...")
  → BUS.send("builder","lead",plan,"plan_approval_request",{request_id})
  → protocol_ctx["waiting_plan"] = request_id
  → 暂停工作，轮询 inbox

Lead: check_inbox → 看到 plan
Lead: review_plan(request_id, approve=True, feedback="looks good")
  → BUS.send("lead","builder","looks good","plan_approval_response",{request_id, approve:True})

Builder: inbox poll 发现 plan_approval_response
  → protocol_ctx["waiting_plan"] = None
  → 恢复工作
```

`match_response()`（L525-535）通过 `request_id` 确保响应匹配正确的请求，防止审批错误的计划。

---

## 六、Task 系统：持久化任务管理

`s20_comprehensive/code.py:70-168`

**要解决的问题**：Agent 需要跨对话轮次跟踪任务状态。`todo_write` 是会话内的临时清单，Task 系统是文件持久化的。

### 6.1 Task 数据结构

```python
@dataclass
class Task:
    id: str                          # "task_{timestamp}_{random4}"
    subject: str                     # 任务标题
    description: str                 # 详细描述
    status: str                      # "pending" | "in_progress" | "completed"
    owner: str | None                # 谁在认领（"agent" / 队友名 / None）
    blockedBy: list[str]             # 依赖的任务 ID 列表
    worktree: str | None = None      # 关联的 worktree 名称
```

存储在 `.tasks/task_{id}.json` 中。

### 6.2 依赖链

任务可以声明它依赖其他任务（`blockedBy`）。`can_start()`（L123-132）检查所有依赖是否已完成：

```python
def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():   # 依赖不存在
            return False
        if load_task(dep_id).status != "completed":  # 依赖未完成
            return False
    return True
```

完成一个任务后，`complete_task()` 会检查并报告被解锁的后续任务（L162-163）。

### 6.3 task_create vs todo_write

| 特性 | todo_write | Task 系统 |
|------|-----------|----------|
| 存储 | 内存 (`CURRENT_TODOS`) | 文件 (`.tasks/*.json`) |
| 生命周期 | 当前会话 | 持久化 |
| 依赖链 | 无 | 支持 blockedBy |
| 认领机制 | 无 | owner + claim |
| Worktree 关联 | 无 | 支持 |

---

## 七、Worktree：工作区隔离

`s20_comprehensive/code.py:171-281`

**要解决的问题**：多个 Agent/队友同时修改文件会冲突。Git worktree 可以为每个任务创建独立的文件系统沙盒。

### 7.1 创建 worktree（L210-231）

```python
def create_worktree(name: str, task_id: str = "") -> str:
    err = validate_worktree_name(name)     # 校验名称合法
    ...
    path = WORKTREES_DIR / name
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if task_id:
        bind_task_to_worktree(task_id, name)  # 关联任务
    return f"Worktree '{name}' created at {path}"
```

每个 worktree 是一个独立的分支（`wt/{name}`），存储在 `.worktrees/{name}/` 下。队友的 `_wt_cwd()` 会将文件操作重定向到 worktree 目录（L638-642）。

### 7.2 删除保护（L253-273）

`remove_worktree` 会检查是否有未提交的变更。有变更时拒绝删除，除非 `discard_changes=True`。

---

## 八、全局对象一览

`s20_comprehensive/code.py` 定义了以下模块级全局对象：

| 全局对象 | 行号 | 类型 | 说明 |
|---------|------|------|------|
| `WORKDIR` | 33 | Path | 工作目录（`Path.cwd()`） |
| `BUS` | 502 | MessageBus | 全局消息总线 |
| `active_teammates` | 503 | dict[str, bool] | 活跃队友字典 |
| `pending_requests` | 518 | dict[str, ProtocolState] | 待处理的协议请求 |
| `SKILL_REGISTRY` | 286 | dict[str, dict] | 已扫描的 skill 注册表 |
| `mcp_clients` | 1514 | dict[str, MCPClient] | 已连接的 MCP 客户端 |
| `background_tasks` | 1220 | dict[str, dict] | 运行中的后台任务 |
| `background_results` | 1221 | dict[str, str] | 已完成的后台任务结果 |
| `scheduled_jobs` | 1302 | dict[str, CronJob] | 已注册的 cron 任务 |
| `cron_queue` | 1303 | list[CronJob] | 等待注入的触发任务 |
| `CURRENT_TODOS` | 76 | list[dict] | 当前会话的 todo 列表 |
| `rounds_since_todo` | 1868 | int | 距上次 todo_write 的轮数 |
| `agent_lock` | 1869 | threading.Lock | agent_loop 的互斥锁 |
| `CLI_ACTIVE` | 54 | bool | CLI 模式标志 |

---

## 速查

| 内容 | 代码位置 |
|------|---------|
| Task 数据类 | L79-87 |
| create_task() | L94-103 |
| claim_task() | L135-153 |
| complete_task() | L156-168 |
| can_start() | L123-132 |
| spawn_subagent() | L1012-1044 |
| SUB_SYSTEM | L953-957 |
| SUB_TOOLS | L960-993 |
| SUB_HANDLERS | L989-993 |
| spawn_teammate_thread() | L606-816 |
| idle_poll() | L567-601 |
| scan_unclaimed_tasks() | L556-564 |
| MessageBus 类 | L480-499 |
| consume_lead_inbox() | L538-547 |
| ProtocolState 数据类 | L507-515 |
| match_response() | L525-535 |
| run_request_shutdown() | L833-841 |
| run_request_plan() | L844-846 |
| run_review_plan() | L849-859 |
| create_worktree() | L210-231 |
| remove_worktree() | L253-273 |
| validate_worktree_name() | L181-189 |
| run_todo_write() | L460-469 |

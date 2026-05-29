# Doc B：工具军团 —— 27 个工具的定义、调度与安全控制

## 读者定位

读者已理解 agent_loop 的执行流（Doc A），现在需要理解"LLM 能调用什么工具、每个工具怎么执行、权限怎么控制"。

---

## 一、工具的定义与注册

`s20_comprehensive/code.py:1681-1846`

s20 有 27 个内置工具，定义在 `BUILTIN_TOOLS` 列表（L1681-1825）。每个工具是一个 dict，包含三个字段：

```python
{"name": "bash",
 "description": "Run a shell command.",     # ← LLM 据此决定何时用这个工具
 "input_schema": {                          # ← LLM 据此生成调用参数
     "type": "object",
     "properties": {"command": {"type": "string"}},
     "required": ["command"]
 }}
```

`name`、`description`、`input_schema` 是 Anthropic API 的工具定义标准格式。LLM 看到这些定义后自行决定调用哪个工具、传什么参数。

### 1.1 为什么要分 `BUILTIN_TOOLS` 和 `BUILTIN_HANDLERS`？

工具定义是给 **LLM 看的**（描述能做什么），handler 是给 **Python 执行的**（实际怎么做）。两者分离的动机：MCP 连接后可以动态追加新工具，而 handler 映射表也随之扩展。如果定义和 handler 耦合在一起，就无法实现运行时工具发现。

### 1.2 工具调度

实际的执行映射在 `BUILTIN_HANDLERS`（L1827-1846）：

```python
BUILTIN_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    "todo_write": run_todo_write, "task": spawn_subagent,
    "load_skill": load_skill,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    ...
}
```

当 agent_loop 遍历 response 中的 tool_use blocks 时（L2002-2003）：

```python
handler = handlers.get(block.name)       # 从映射表取 handler 函数
output = call_tool_handler(handler, block.input, block.name)  # 调用
```

`call_tool_handler()`（L451-457）做了两件事：参数解包 + 异常保护：

```python
def call_tool_handler(handler, args: dict, name: str) -> str:
    if not handler:
        return f"Unknown: {name}"
    try:
        return handler(**(args or {}))    # 将 LLM 传来的 dict 解包为关键字参数
    except TypeError as e:
        return f"Error: {e}"
```

---

## 二、工具分类速查

按功能域分为 6 组：

### 文件操作（5 个）

| 工具 | handler | 说明 |
|------|---------|------|
| `bash` | `run_bash()` L389 | 执行 shell 命令，输出截断 50000 字符，超时 120s |
| `read_file` | `run_read()` L401 | 读取文件，支持 `limit` 和 `offset` 分页 |
| `write_file` | `run_write()` L415 | 写入文件，自动创建父目录 |
| `edit_file` | `run_edit()` L425 | 精确文本替换（首次匹配），底层用 `str.replace` |
| `glob` | `run_glob()` L438 | 文件模式匹配，限定在工作目录内 |

全部通过 `safe_path()`（L379-386）校验路径不逃逸工作目录。

### 任务管理（6 个）

| 工具 | handler | 说明 |
|------|---------|------|
| `todo_write` | `run_todo_write()` L460 | 更新当前会话的轻量任务列表（内存，非持久化） |
| `create_task` | `run_create_task()` L1618 | 创建持久化任务（JSON 文件存储在 `.tasks/`） |
| `list_tasks` | `run_list_tasks()` L1626 | 列出所有任务及其状态 |
| `get_task` | `run_get_task()` L1636 | 获取单个任务的完整 JSON |
| `claim_task` | `run_claim_task()` L1642 | 认领 pending 任务（设置 owner + in_progress） |
| `complete_task` | `run_complete_task()` L1648 | 完成任务，自动解锁被它阻塞的后续任务 |

`todo_write` vs Task 系统的区别：
- `todo_write` = 当前会话的临时清单（存在 `CURRENT_TODOS` 列表里），退出即消失
- Task 系统 = 持久化到 `.tasks/task_*.json`，支持依赖链（blockedBy）、认领、隔离（worktree）

### 子Agent与队友（4 个）

| 工具 | handler | 说明 |
|------|---------|------|
| `task` | `spawn_subagent()` L1012 | 同步子 Agent，最多 30 轮，返回摘要 |
| `spawn_teammate` | `run_spawn_teammate()` L1654 | 启动自主队友线程，持续工作直到 idle timeout |
| `send_message` | `run_send_message()` L1657 | 向指定队友发消息（通过 MessageBus） |
| `check_inbox` | `run_check_inbox()` L1661 | 读取并清空 lead 的收件箱 |

### 协议控制（3 个）

| 工具 | handler | 说明 |
|------|---------|------|
| `request_shutdown` | `run_request_shutdown()` L833 | 请求指定队友关闭（protocol） |
| `request_plan` | `run_request_plan()` L844 | 请求队友提交执行计划 |
| `review_plan` | `run_review_plan()` L849 | 批准或拒绝队友提交的计划 |

### 调度与集成（5 个）

| 工具 | handler | 说明 |
|------|---------|------|
| `schedule_cron` | `run_schedule_cron()` L1459 | 创建定时任务（5 字段 cron 表达式） |
| `list_crons` | `run_list_crons()` L1467 | 列出所有定时任务 |
| `cancel_cron` | `run_cancel_cron()` L1479 | 取消指定定时任务 |
| `connect_mcp` | `run_connect_mcp()` L1673 | 连接到 MCP 服务器，发现其工具 |
| `load_skill` | `load_skill()` L335 | 加载 skill 的完整内容到上下文 |

### 工作区隔离（3 个）

| 工具 | handler | 说明 |
|------|---------|------|
| `create_worktree` | `run_create_worktree()` L1606 | 创建 git worktree（隔离分支） |
| `remove_worktree` | `run_remove_worktree()` L1609 | 删除 worktree（有变更时拒绝，除非 `discard_changes=True`） |
| `keep_worktree` | `run_keep_worktree()` L1612 | 保留 worktree 供人工审查 |

### 上下文控制（1 个）

| 工具 | 特殊处理 | 说明 |
|------|---------|------|
| `compact` | agent_loop 内直接拦截（L1979-1984） | 不在 TOOL_HANDLERS 中，由 agent_loop 特殊处理：调用 `compact_history()` 后 `break` 当前工具循环 |

---

## 三、权限管线：工具执行前的安全检查

`s20_comprehensive/code.py:883-912`

权限控制通过 `permission_hook` 回调实现，注册在 `PreToolUse` 事件上。它在工具 handler **执行之前**运行，可以返回错误字符串来阻止执行。

```python
def permission_hook(block):
    if block.name == "bash":
        # 第一层：绝对禁止（DENY_LIST）
        for pattern in ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]:
            if pattern in block.input.get("command", ""):
                return f"Permission denied: '{pattern}' is on the deny list"

        # 第二层：危险命令人工确认（DESTRUCTIVE）
        if any(token in command for token in ["rm ", "> /etc/", "chmod 777"]):
            choice = input("  Allow? [y/N] ")
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    if block.name in ("write_file", "edit_file"):
        # 第三层：路径不逃逸工作目录
        try:
            safe_path(block.input.get("path", ""))
        except Exception:
            return f"Permission denied: path escapes workspace"

    if block.name.startswith("mcp__") and "deploy" in block.name:
        # 第四层：MCP 破坏性工具人工确认
        choice = input("  Allow? [y/N] ")
        if choice not in ("y", "yes"):
            return "Permission denied by user"

    return None  # ← 放行
```

四层防线：自动拒绝（DENY_LIST）→ 人工确认（DESTRUCTIVE）→ 路径校验（safe_path）→ MCP 确认。

返回非 None 值时，`trigger_hooks("PreToolUse", block)` 短路返回，`log_hook` 不执行，工具的 handler 也不执行。agent_loop 将返回值作为 tool_result 的 content 传回 LLM（L1988-1990），LLM 能看到"权限被拒绝"的消息。

---

## 四、MCP 工具的动态发现

`s20_comprehensive/code.py:1487-1601`

MCP（Model Context Protocol）允许 Agent 在运行时连接到外部服务器并发现其工具。s20 提供两个 mock 服务器作为教学示例。

### 4.1 定义 mock 服务器（L1524-1567）

```python
def _mock_server_docs():
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search documentation. (readOnly)", ...},
            {"name": "get_version", "description": "Get API version. (readOnly)", ...},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client
```

### 4.2 工具池组装（L1585-1601）

每轮 agent_loop 都会调用 `assemble_tool_pool()`，将 builtin 工具和所有已连接的 MCP 工具合并：

```python
def assemble_tool_pool():
    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)
    for server_name, mcp_client in mcp_clients.items():
        for tool_def in mcp_client.tools:
            prefixed = f"mcp__{server_name}__{tool_def['name']}"
            tools.append({...})
            handlers[prefixed] = (...)
    return tools, handlers
```

MCP 工具的命名约定：`mcp__{server}__{tool}`，例如 `mcp__docs__search`。`permission_hook` 通过检查 `block.name.startswith("mcp__")` 来识别 MCP 工具并施加额外安全检查。

### 4.3 为什么每轮都要重新组装？

因为 Agent 可以在对话中调用 `connect_mcp` 连接新服务器（L1570-1582），`mcp_clients` 字典会变化。每轮重建 `tools` 和 `handlers` 保证 LLM 始终看到最新的工具列表。

---

## 五、Skill 加载：按需注入专业知识

`s20_comprehensive/code.py:284-341`

Skill 是项目 `skills/` 目录下的 Markdown 文件（`SKILL.md`）。`load_skill(name)` 将完整内容注入到消息中，让 LLM 获得特定领域的专业知识。

```python
def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}. Available: {', '.join(SKILL_REGISTRY.keys())}"
    return skill["content"]   # 返回完整的 SKILL.md 文本
```

Skill 列表也出现在 system prompt 中（L367-368），让 LLM 知道有哪些可用的 skill 并在需要时主动调用 `load_skill`。

---

## 速查

| 内容 | 代码位置 |
|------|---------|
| BUILTIN_TOOLS 定义（27 个） | L1681-1825 |
| BUILTIN_HANDLERS 映射 | L1827-1846 |
| call_tool_handler() | L451-457 |
| permission_hook()（四层防线） | L887-912 |
| assemble_tool_pool()（MCP 合并） | L1585-1601 |
| MCPClient 类 | L1491-1513 |
| connect_mcp() | L1570-1582 |
| scan_skills() + SKILL_REGISTRY | L303-321 |
| load_skill() | L335-340 |
| safe_path()（路径校验） | L379-386 |
| run_bash() | L389-398 |
| run_read() | L401-412 |
| run_write() | L415-422 |
| run_edit() | L425-435 |
| agent_loop 中 compact 特殊处理 | L1979-1984 |
| agent_loop 中 background 分流 | L1993-1999 |

# Doc D：容错与并发 —— 错误恢复、重试、后台任务与定时调度

## 读者定位

读者已理解 Agent Loop 的执行流和上下文管线。现在需要理解"API 调用失败怎么办、慢任务怎么后台执行、定时任务怎么触发"。

---

## 一、错误恢复：RecoveryState 状态机

`s20_comprehensive/code.py:1164-1170`

```python
class RecoveryState:
    def __init__(self):
        self.has_escalated = False           # 是否已升级过 max_tokens
        self.recovery_count = 0              # 已追加 CONTINUATION_PROMPT 的次数
        self.consecutive_529 = 0             # 连续 529（过载）次数
        self.has_attempted_reactive_compact = False  # 是否已尝试过被动压缩
        self.current_model = PRIMARY_MODEL    # 当前使用的模型（故障转移用）
```

这是一个简单的状态机，在 `agent_loop` 的局部作用域内创建（L1915），贯穿整个 agent_loop 生命周期。不跨回合 —— 每次进入 `agent_loop()` 都是新的 state。

---

## 二、with_retry()：带重试的 API 调用

`s20_comprehensive/code.py:1178-1205`

**要解决的问题**：API 可能因为限流（429）、过载（529）、网络波动等临时故障而失败。直接重试可以自动恢复，不需要告知用户。

```python
MAX_RETRIES = 3
BASE_DELAY_MS = 500          # 初始延迟 0.5 秒

def with_retry(fn, state: RecoveryState):
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0    # 成功 → 重置 529 计数器
            return result
        except Exception as e:
            name = type(e).__name__.lower()
            msg = str(e).lower()

            if "ratelimit" in name or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  [429] retry {attempt+1}/{MAX_RETRIES} after {delay:.1f}s")
                time.sleep(delay)
                continue                            # ← 重试

            if "overloaded" in name or "529" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= 2 and FALLBACK_MODEL:
                    state.current_model = FALLBACK_MODEL  # 切换备用模型
                delay = retry_delay(attempt)
                time.sleep(delay)
                continue                            # ← 重试

            raise   # 非临时故障，直接抛出，不重试
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")
```

### 2.1 退避策略（L1173-1175）

```python
def retry_delay(attempt: int) -> float:
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000  # 指数退避，上限 32 秒
    return base + random.uniform(0, base * 0.25)               # 加随机抖动
```

| 尝试次数 | 基础延迟 | 加抖动后范围 |
|---------|---------|------------|
| attempt=0 | 500ms | 500~625ms |
| attempt=1 | 1000ms | 1000~1250ms |
| attempt=2 | 2000ms | 2000~2500ms |

抖动（jitter）的作用：多个并发请求在同一时刻失败时，不会同时重试（雷群效应）。

### 2.2 故障转移

连续 2 次 529 过载错误后，自动切换到 `FALLBACK_MODEL`（L1195-1197）。`call_llm()` 使用 `state.current_model` 而不是全局 `MODEL`（L1904），所以切换对调用方透明。

### 2.3 `with_retry` 与 `call_llm` 的配合

```python
# L1899-1909
def call_llm(messages, context, tools, state, max_tokens):
    system = assemble_system_prompt(context)
    return with_retry(
        lambda: client.messages.create(        # fn 是一个 lambda（延迟执行）
            model=state.current_model,          # ← 使用 state 上可能被故障转移修改的 model
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens),
        state)
```

`fn` 是一个闭包，捕获了 `system`, `messages`, `tools`, `max_tokens`。重试时这些值不变，但 `state.current_model` 可能已经被 529 处理逻辑修改了。

---

## 三、max_tokens 升级

`s20_comprehensive/code.py:1952-1963`

**要解决的问题**：LLM 的回复可能在输出中途被截断（`stop_reason == "max_tokens"`），导致工具调用不完整或文本被截断。

两级恢复：

```
stop_reason == "max_tokens"
  │
  ├─ 第 1 次：has_escalated=False
  │   → max_tokens = 16000（升级），重新调用 LLM
  │
  └─ 第 2+ 次：has_escalated=True
      → 追加 assistant 消息 + CONTINUATION_PROMPT，要求 LLM 继续
      → recovery_count 递增
      → 最多 recovery_count=2 次
```

```python
if response.stop_reason == "max_tokens":
    if not state.has_escalated:
        max_tokens = ESCALATED_MAX_TOKENS     # 8000 → 16000
        state.has_escalated = True
        continue                                # ← 用更大的 max_tokens 重试

    messages.append({"role": "assistant", "content": response.content})
    if state.recovery_count < MAX_RECOVERY_RETRIES:  # < 2
        messages.append({"role": "user", "content": CONTINUATION_PROMPT})
        state.recovery_count += 1
        continue                                # ← 追加 "继续" 提示后重试
    return                                      # ← 放弃
```

每次成功（`stop_reason != "max_tokens"`）后，这些状态被重置（L1965-1966）：

```python
max_tokens = DEFAULT_MAX_TOKENS
state.has_escalated = False
```

---

## 四、后台任务：慢操作的异步执行

`s20_comprehensive/code.py:1215-1283`

**要解决的问题**：`pip install`、`npm install`、编译等操作可能需要几分钟。如果在 agent_loop 内同步等待，整个 Agent 会卡住。

**怎么做**：将慢命令派发到后台线程，立即返回占位符 `tool_result`。完成后通过通知消息注入结果。

### 4.1 判断什么该后台执行（L1225-1238）

```python
def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    command = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(keyword in command for keyword in slow_keywords)

def should_run_background(tool_name, tool_input):
    return bool(tool_input.get("run_in_background")) or is_slow_operation(...)
```

两种触发方式：LLM 显式传 `run_in_background=True`（bash 工具的 schema 中有这个参数），或命令关键词自动匹配。

### 4.2 启动后台任务（L1241-1263）

```python
def start_background_task(block, handlers):
    bg_id = f"bg_{_bg_counter:04d}"
    command = block.input.get("command", block.name)

    def worker():
        handler = handlers.get(block.name)
        result = call_tool_handler(handler, block.input, block.name)
        trigger_hooks("PostToolUse", block, result)    # 仍然触发钩子
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = str(result)

    threading.Thread(target=worker, daemon=True).start()
    return bg_id
```

### 4.3 收集中完成结果（L1266-1283）

```python
def collect_background_results() -> list[str]:
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items()
                 if task["status"] == "completed"]
    notifications = []
    for bg_id in ready:
        task = background_tasks.pop(bg_id)
        output = background_results.pop(bg_id, "")
        notifications.append(f"<task_notification>...</task_notification>")
    return notifications
```

收集时机：
- `agent_loop` 每轮开始前通过 `inject_background_notifications()` 注入（L1927）
- `agent_loop` 工具结果组装时通过 `build_user_content()` 注入（L1882-1889）

### 4.4 全局变量与线程安全

```python
background_tasks: dict[str, dict] = {}       # 模块级
background_results: dict[str, str] = {}      # 模块级
background_lock = threading.Lock()           # 保护两个 dict 的互斥锁
```

`daemon=True` 确保主线程退出时后台线程自动终止。

---

## 五、Cron 调度器：定时任务系统

`s20_comprehensive/code.py:1286-1484`

**要解决的问题**：Agent 需要在特定时间点主动执行任务（如"每天早上检查 CI 状态"），而不是被动等待用户输入。

### 5.1 架构：两个线程

```
cron_scheduler_loop()   ← daemon 线程，每秒检查 cron 表达式
    │
    ├─ 匹配 → 放入 cron_queue
    │
    ▼
consume_cron_queue()    ← agent_loop 每轮调用，从队列取出
    │
    ▼
messages.append(...)    ← 作为 user 消息注入对话
    │
    ▼
agent_loop 正常处理     ← Agent 看到 "[Scheduled] ..." 后自主行动
```

此外还有 `cron_autorun_loop`（L2030-2045），在 idle 状态下绕过 REPL 直接调用 `agent_loop`。它与主线程通过 `agent_lock` 同步。

### 5.2 Cron 表达式解析（L1308-1375）

支持 5 字段标准格式：`minute hour day-of-month month day-of-week`。实现了：
- 通配符 `*`
- 步进 `*/5`（每 5 分钟）
- 枚举 `1,3,5`
- 范围 `1-5`

### 5.3 持久化（L1390-1405）

标记 `durable=True` 的 job 会序列化到 `.scheduled_tasks.json`，Agent 重启后自动加载。

### 5.4 Tool 接口

Agent 通过三个工具操作 cron：
- `schedule_cron(cron, prompt, recurring, durable)` → 创建 job
- `list_crons()` → 列出所有 job
- `cancel_cron(job_id)` → 取消 job

---

## 六、Todo 提醒机制

`s20_comprehensive/code.py:1868`

```python
rounds_since_todo = 0   # 模块级全局
```

在 agent_loop 中（L1929-1932）：

```python
if rounds_since_todo >= 3:
    messages.append({"role": "user",
                     "content": "<reminder>Update your todos.</reminder>"})
    rounds_since_todo = 0
```

每次工具调用后（L2007-2010）：

```python
if block.name == "todo_write":
    rounds_since_todo = 0    # 重置
else:
    rounds_since_todo += 1   # 递增
```

逻辑：如果连续 3 轮（3 个工具调用）没用 `todo_write`，就提醒 LLM 更新 todo 列表。

---

## 速查

| 内容 | 代码位置 |
|------|---------|
| RecoveryState 类 | L1164-1170 |
| with_retry() | L1178-1205 |
| retry_delay() | L1173-1175 |
| is_prompt_too_long_error() | L1208-1212 |
| call_llm() | L1899-1909 |
| max_tokens 升级逻辑 | L1952-1963 |
| is_slow_operation() | L1225-1232 |
| should_run_background() | L1235-1238 |
| start_background_task() | L1241-1263 |
| collect_background_results() | L1266-1283 |
| inject_background_notifications() | L1892-1896 |
| build_user_content() | L1882-1889 |
| CronJob 数据类 | L1293-1299 |
| cron_matches() | L1323-1342 |
| schedule_job() | L1407-1420 |
| cron_scheduler_loop() | L1433-1449 |
| consume_cron_queue() | L1452-1455 |
| cron_autorun_loop() | L2030-2045 |
| rounds_since_todo 逻辑 | L1929-1932, L2007-2010 |
| agent_lock | L1869 |

# Doc A：入口与循环 —— main() → agent_loop() 全景解读

## 读者定位

读者已完成 s01-s05（agent loop、tool dispatch、permission、hooks、todo），现在直接阅读 s20 最终版代码，需要理解从 `main()` 入口到 `agent_loop()` 内每一轮迭代的完整执行流。

---

## 一、main()：外层的 REPL 循环

`s20_comprehensive/code.py:2048-2097`

`main()` 负责三件事：初始化 → REPL 循环 → 收尾。

### 1.1 初始化阶段（L2048-2064）

```python
if __name__ == "__main__":
    CLI_ACTIVE = True

    # 项目根目录加入搜索路径（支持 uv run 等非 cwd 启动方式）
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    # Trace 系统：通过 hook 自动记录所有交互到 logs/traces/
    from trace.trace_hooks import enable_trace as enable_trace_logging
    trace_logger, _trace_step = enable_trace_logging(
        register_hook, trace_dir="logs/traces", enabled=False)

    history = []          # 完整的消息历史，在 REPL 循环中持续增长
    context = update_context({}, [])  # 动态上下文 dict（memory、MCP、teammates）

    # 启动后台 cron 线程，持续检查定时任务
    threading.Thread(target=cron_autorun_loop,
                     args=(history, context), daemon=True).start()
```

关键变量：

| 变量 | 类型 | 生命周期 | 说明 |
|------|------|---------|------|
| `CLI_ACTIVE` | `bool` | 全局（模块级） | 控制 `terminal_print` 是否走 readline 兼容路径 |
| `history` | `list[dict]` | 整个会话 | Anthropic Messages API 格式的消息列表 |
| `context` | `dict` | 整个会话 | `{"memories": str, "connected_mcp": list, "active_teammates": list}` |
| `trace_logger` | `AgentLogger \| None` | 整个会话 | `enabled=False` 时为 None，Trace 完全关闭 |

### 1.2 REPL 循环（L2065-2093）

```python
    try:
        while True:
            query = input(PROMPT)          # ① 读取用户输入
            ...
            trigger_hooks("UserPromptSubmit", query)  # ② 触发钩子
            turn_start = len(history)      # ③ 记录本轮开始位置
            history.append({"role": "user", "content": query})  # ④ 追加到 history

            with agent_lock:               # ⑤ 获取锁（cron 线程也可能调 agent_loop）
                agent_loop(history, context)      # ⑥ 核心循环
                context = update_context(context, history)
                print_turn_assistants(history, turn_start)  # ⑦ 打印 AI 回复

            inbox = consume_lead_inbox(...) # ⑧ 检查队友消息
            if inbox: ...
            print()
    finally:
        if trace_logger:
            trace_logger.finalize()        # ⑨ 写入 SESSION_SUMMARY，关闭文件
```

**执行顺序**：每轮对话走一遍 ①→⑧。`agent_loop()` 内部可能有多次 LLM 调用（工具循环），但对外层 REPL 来说是一次"回合"。

**`agent_lock` 的必要性**：后台 cron 线程在定时触发时也会调用 `agent_loop(history, context)`（[code.py:2043](s20_comprehensive/code.py#L2043)）。锁保证同一时刻只有一个线程在操作 `history`。

### 1.3 为什么 `print_turn_assistants` 而不是在 loop 内部打印？

因为 `agent_loop()` 可能被 cron 线程调用，此时不应向终端输出（用户正在输入）。将"执行"和"展示"分离，由调用方决定何时展示结果。

---

## 二、agent_loop()：核心循环

`s20_comprehensive/code.py:1912-2018`

这是整个项目的精髓。每一轮迭代走 6 个阶段：

```
┌─────────────────────────────────────────────────────┐
│  agent_loop(messages, context)                       │
│                                                      │
│  ① 注入阶段：cron / background / todo reminder       │
│  ② 准备阶段：prepare_context + prompt assembly        │
│  ③ LLM 调用：call_llm() → with_retry() → API        │
│  ④ 响应处理：stop_reason 分支（max_tokens / end_turn）│
│  ⑤ 工具循环：PreToolUse → handler → PostToolUse      │
│  ⑥ 结果组装：build_user_content → 下一轮             │
└─────────────────────────────────────────────────────┘
```

### 阶段 ①：注入（L1918-1932）

在调用 LLM 之前，向 messages 注入三类"被动消息"：

```python
fired = consume_cron_queue()       # cron 定时触发的 prompt
for job in fired:
    messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})

inject_background_notifications(messages)  # 后台任务完成通知

if rounds_since_todo >= 3:         # 每 3 轮无 todo 操作就提醒
    messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
```

这三类消息对 LLM 来说都是"用户发来的"，会驱动 Agent 做出响应。

### 阶段 ②：准备（L1934-1936）

```python
prepare_context(messages)           # 上下文压缩管线（详见 Doc C）
context = update_context(context, messages)  # 刷新 memory/MCP/teammate 信息
tools, handlers = assemble_tool_pool()      # 合并 builtin + MCP 工具
```

### 阶段 ③：LLM 调用（L1938-1950）

```python
trigger_hooks("PreModelCall", messages, context, tools)  # Trace 钩子
try:
    response = call_llm(messages, context, tools, state, max_tokens)
except Exception as e:
    trigger_hooks("PostModelCall", None, messages)        # Trace 错误钩子
    if is_prompt_too_long_error(e) ...:                   # 上下文过长 → reactive compact
        continue
    messages.append({"role": "assistant", "content": [
        {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
    return
trigger_hooks("PostModelCall", response, messages)        # Trace 成功钩子
```

`call_llm()` 内部做了两件事（L1899-1909）：
1. 调用 `assemble_system_prompt(context)` 构建 system prompt
2. 调用 `with_retry(fn, state)` 执行 API 调用（带重试逻辑，详见 Doc D）

### 阶段 ④：响应处理（L1952-1970）

```python
if response.stop_reason == "max_tokens":      # 输出被截断
    if not state.has_escalated:
        max_tokens = ESCALATED_MAX_TOKENS      # 8000 → 16000 重试
        state.has_escalated = True
        continue
    # 已升级仍然不够 → 追加 CONTINUATION_PROMPT
    messages.append({"role": "assistant", "content": response.content})
    if state.recovery_count < MAX_RECOVERY_RETRIES:
        messages.append({"role": "user", "content": CONTINUATION_PROMPT})
        continue
    return

messages.append({"role": "assistant", "content": response.content})
if not has_tool_use(response.content):         # 纯文本回复，无工具调用
    trigger_hooks("Stop", messages)
    return                                      # ← 退出循环，返回外层 REPL
```

`stop_reason` 的三种分支：
- `"end_turn"` + 无 tool_use → 退出，打印文本回复
- `"end_turn"` + 有 tool_use → 进入阶段 ⑤ 执行工具
- `"max_tokens"` → 升级 token 预算或追加 CONTINUATION_PROMPT

### 阶段 ⑤：工具循环（L1972-2013）

```python
results = []
for block in response.content:
    if block.type != "tool_use":
        continue

    if block.name == "compact":                # 手动压缩
        messages[:] = compact_history(messages)
        compacted_now = True
        break

    blocked = trigger_hooks("PreToolUse", block)  # permission_hook 在此拦截
    if blocked:
        results.append({... "content": str(blocked)})  # 被拒绝的工具返回错误消息
        continue

    if should_run_background(block.name, block.input):  # 慢操作 → 后台线程
        bg_id = start_background_task(block, handlers)
        ... continue

    handler = handlers.get(block.name)
    output = call_tool_handler(handler, block.input, block.name)  # 实际执行
    trigger_hooks("PostToolUse", block, output)    # large_output_hook 在此

    if block.name == "todo_write":
        rounds_since_todo = 0    # 重置提醒计数器
    else:
        rounds_since_todo += 1

    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
```

三种特殊分流：
- **`compact`**：不走正常工具执行，直接压缩上下文后 `break`
- **`should_run_background`**：慢 bash 命令 → 后台线程，返回占位符 `tool_result`
- **`todo_write`**：重置 `rounds_since_todo` 计数器

### 阶段 ⑥：结果组装（L2015-2018）

```python
if compacted_now:
    continue  # compact 后直接进入下一轮
messages.append({"role": "user", "content": build_user_content(results)})
```

`build_user_content()` 将"工具结果"和"后台任务完成通知"合并为一条 user 消息，拼在 tool_results 之前：

```python
def build_user_content(results):
    content = []
    for note in collect_background_results():
        content.append({"type": "text", "text": note})  # 后台任务通知在前
    content.extend(results)                               # 本轮工具结果在后
    return content
```

---

## 三、Hooks 在 agent_loop 中的触发点总览

`s20_comprehensive/code.py:866-948`

| 触发点 | 触发代码行 | 回调注册行 | 已注册的回调 |
|--------|----------|----------|------------|
| 用户输入后 | L2073（main） | L944 | `user_prompt_hook` |
| LLM 调用前 | L1938 | （Trace 注册，enabled=False 时为空） | — |
| LLM 调用后 | L1942/1950 | （同上） | — |
| 工具执行前 | L1986 | L945-946 | `permission_hook`, `log_hook` |
| 工具执行后 | L2004 | L947 | `large_output_hook` |
| 循环退出前 | L1969 | L948 | `stop_hook` |

**触发 → 执行 → 短路** 的完整链路：

```python
# 定义（L866）
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], ..., "Stop": []}

# 注册（L944-948）
register_hook("PreToolUse", permission_hook)  # 先执行
register_hook("PreToolUse", log_hook)          # 后执行

# 触发（L875-880）
def trigger_hooks(event, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:    # ← 非 None 立即短路
            return result
    return None
```

`permission_hook` 返回非 None 时，`log_hook` 不会被调用。所有 Trace 回调都返回 None，永远不会短路流程。

---

## 四、关键变量生命周期

| 变量 | 定义行 | 作用域 | 被修改的位置 |
|------|--------|--------|------------|
| `history` | L2061 | main() 局部 | main L2075, L2091；agent_loop L1923,1930,1947,1958,1967,1980,2012,2018 |
| `context` | L2062 | main() 局部 | update_context() L1855；agent_loop L1935 |
| `state` | L1915 | agent_loop() 局部 | RecoveryState 内部属性被 with_retry 修改 |
| `max_tokens` | L1916 | agent_loop() 局部 | L1954（升级为 16000），L1965（重置为 8000） |
| `rounds_since_todo` | L1868 | 全局（模块级） | L2007-2010（每个工具调用后递增，todo_write 时重置） |
| `tools, handlers` | L1914 | agent_loop() 局部 | L1936（每轮重建，因为 MCP 工具可能变化） |

---

## 速查：执行流中的关键函数

| 函数 | 行号 | 被谁调用 | 一句话 |
|------|------|---------|--------|
| `agent_loop()` | 1912 | main(), cron_autorun_loop() | 核心循环 |
| `call_llm()` | 1899 | agent_loop() | 组装 system prompt + 带重试的 API 调用 |
| `with_retry()` | 1178 | call_llm() | 指数退避重试（429/529） |
| `prepare_context()` | 1872 | agent_loop() | 三层压缩管线 |
| `assemble_system_prompt()` | 360 | call_llm() | 从 context dict 构建 system prompt |
| `assemble_tool_pool()` | 1585 | agent_loop() | 合并 builtin + MCP 工具 |
| `update_context()` | 1855 | agent_loop(), main() | 刷新 memory/MCP/teammate 快照 |
| `build_user_content()` | 1882 | agent_loop() | 组装 user 消息（后台通知 + 工具结果） |
| `print_turn_assistants()` | 2021 | main(), cron_autorun_loop() | 打印本轮 AI 的文本回复 |
| `inject_background_notifications()` | 1892 | agent_loop() | 注入已完成的后台任务通知 |
| `consume_cron_queue()` | 1452 | agent_loop() | 获取到期的定时任务 |
| `has_tool_use()` | 1005 | agent_loop(), spawn_subagent() | 判断响应中是否包含工具调用 |
| `extract_text()` | 996 | 多处 | 从 content blocks 中提取纯文本 |
| `terminal_print()` | 57 | print_turn_assistants, BUS, 多处 | 线程安全的终端打印 |

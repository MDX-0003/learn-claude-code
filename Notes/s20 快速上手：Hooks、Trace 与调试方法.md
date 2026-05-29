# s20 快速上手：Hooks、Trace 与调试方法

## 一、Hooks：Agent Loop 的扩展点

### 它要解决什么问题？

Agent Loop 的核心逻辑很简单：调 LLM → 执行工具 → 把结果喂回去 → 循环。但你总需要在循环的不同阶段插入额外的逻辑 —— 比如在工具执行前检查权限、在工具执行后警告大输出、在循环结束时打印统计。如果把所有这些逻辑硬编码在循环里，循环体会越来越臃肿，而且"权限检查"和"日志记录"跟 Agent 的核心任务毫无关系。

Hooks 就是解决这个问题的：**它让你在不修改 Agent Loop 主体的情况下，往循环的关键节点上"挂"回调函数。**

类比：像一个可插拔的插座。Agent Loop 在不同的位置提供了插座（钩子事件），你可以随时把某个电器（回调函数）插上去或拔下来，不影响循环本身。

### 钩子事件有哪些？

定义在 `s20_comprehensive/code.py:866`：



```python
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [],
         "PostToolUse": [], "Stop": [],
         "PreModelCall": [], "PostModelCall": []}
```

每个 key 是一个**事件名称**，value 是一个**回调函数列表**。事件触发时，列表中所有函数依次执行。

| 事件               | 触发时机                               | 回调签名                     | 能做什么                                 |
| ------------------ | -------------------------------------- | ---------------------------- | ---------------------------------------- |
| `UserPromptSubmit` | 用户在 REPL 输入后、进入 agent_loop 前 | `(query: str)`               | 记录用户输入                             |
| `PreModelCall`     | LLM 调用前                             | `(messages, context, tools)` | 记录当前上下文快照                       |
| `PostModelCall`    | LLM 返回后                             | `(response, messages)`       | 记录 token 用量、提取输出                |
| `PreToolUse`       | 每个工具执行前                         | `(block)`                    | **权限控制**（返回非 None 值可阻止执行） |
| `PostToolUse`      | 每个工具执行后                         | `(block, output)`            | 记录结果、警告大输出                     |
| `Stop`             | agent_loop 即将退出时                  | `(messages)`                 | 打印统计、收尾工作                       |

它们在 Agent Loop 中的触发位置（`s20_comprehensive/code.py:1912-1970`）：



```
UserPromptSubmit(query)
    │
    ▼
  agent_loop() 内部:
    │
    ├─ PreModelCall(messages, context, tools)
    │     │
    │     ▼  调用 LLM
    │     │
    │     ├─ PostModelCall(response, messages)
    │
    ├─ for each tool_use block:
    │     ├─ PreToolUse(block)  ← 若返回非 None，阻止此工具
    │     ├─ 执行工具
    │     └─ PostToolUse(block, output)
    │
    └─ Stop(messages)  ← 退出前
```

### 如何注册和触发？

两个函数，代码在 `s20_comprehensive/code.py:871-880`：



```python
def register_hook(event: str, callback):
    """把 callback 注册到 event 的回调列表里"""
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    """触发 event，依次调用所有回调。若某个回调返回非 None，立即返回该值"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None
```
触发trigger_hooks时用同一套args触发了多个callback，这意味着**同event内**的多个callback，在定义时已经确保了参数完全一致

不同event触发时传的参数则不一样，取决于"这个事件发生时有哪些信息可用"：

| 事件               | 触发代码                                                  | 回调收到的参数             |
| ------------------ | --------------------------------------------------------- | -------------------------- |
| `UserPromptSubmit` | `trigger_hooks("UserPromptSubmit", query)`                | `query: str`               |
| `PreModelCall`     | `trigger_hooks("PreModelCall", messages, context, tools)` | `messages, context, tools` |
| `PostModelCall`    | `trigger_hooks("PostModelCall", response, messages)`      | `response, messages`       |
| `PreToolUse`       | `trigger_hooks("PreToolUse", block)`                      | `block`（tool_use 对象）   |
| `PostToolUse`      | `trigger_hooks("PostToolUse", block, output)`             | `block, output`            |
| `Stop`             | `trigger_hooks("Stop", messages)`                         | `messages`                 |

所以本质上这是一个**约定**：触发方和回调之间，通过事件名称约定参数数量和含义。没有类型检查，没有接口定义——全靠代码约定。Python 的 `*args` 机制让这个系统极度灵活但也失去了编译期安全。

关键设计：**`trigger_hooks` 在第一个返回非 None 的回调处Return**。这意味着 某些hook函数如`PreToolUse` 上的 `permission_hook` 可以通过返回一个错误字符串来阻止后续callback执行，而后续的 `log_hook` 就收不到这个事件了。

所以注册顺序影响了`trigger_hooks`内的callback调用顺序 —— 当前代码（L944-948）把 `permission_hook` 注册在 `log_hook` 前面：

```python
register_hook("PreToolUse", permission_hook)  # 先做权限检查
register_hook("PreToolUse", log_hook)          # 通过了才记录
```

我们设计的Trace Hook则不同，尽管大部分hook都是有实际意义的（影响流程分支），但我们的Trace独立于流程外，只负责log。实现方法也很简单，只返回None，保证`trigger_hooks`触发Trace CallBack以后，不会因为其返回值中断其他CallBack的执行。

### 你刚看到的运行示例

你输入 `列出你的tools` 后：

1. `UserPromptSubmit` 触发 → `user_prompt_hook` 打印 `[HOOK] UserPromptSubmit: F:\...`
2. Agent Loop 内部调用 LLM
3. LLM 返回纯文本（无 tool_use）
4. `Stop` 触发 → `stop_hook` 打印 `[HOOK] Stop: 0 tool result(s)`
5. 返回值，主循环打印 AI 的回复

这就是 hooks 的完整生命周期。

------

## 二、Trace 系统：你能看到什么？需要手动加代码吗？

### Trace 不是 `print`，它是结构化的"行车记录仪"

普通的 `print()` 和 `[HOOK]` 消息只能在终端看一眼，程序退出后就没了。Trace 系统做的事：**把 Agent 运行过程中的每一步（LLM 调了什么、工具返回了什么、用了多少 token）自动写入本地文件**，产生两种格式：

- **JSONL**（`logs/traces/trace-*.jsonl`）：每行一个 JSON，可以导入 pandas 做分析
- **HTML**（`logs/traces/trace-*.html`）：浏览器打开，按 step 分组的时间线视图

### 它自动记录什么？（不需要你手动加任何东西）

当前 Trace（`trace/trace_hooks.py` + `trace/agent_logger.py`）通过在 6 个钩子事件上注册回调，**自动捕获以下内容**：

| 自动记录的内容                                         | 对应钩子                |
| ------------------------------------------------------ | ----------------------- |
| 用户每次输入了什么                                     | `UserPromptSubmit`      |
| LLM 调用前的 messages 快照（截断到 300 字符/条）       | `PreModelCall`          |
| LLM 返回的文本 + token 消耗（prompt/completion/total） | `PostModelCall`         |
| 每个工具的名称和参数                                   | `PreToolUse`            |
| 每个工具的返回结果（截断到 50000 字符）                | `PostToolUse`           |
| 会话统计：总步数、工具调用次数、总 token 数            | `finalize()` 时自动生成 |

**你不需要在代码中手动添加任何 `print` 或 `log_event` 调用**。只要 `enabled=True`（[s20_comprehensive/code.py:2057](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/s20_comprehensive/code.py#L2057)），所有内容自动记录。

### 如果你想 Trace 一个自定义变量

Trace 系统只自动记录事件级别的数据。如果你想看某个特定变量的值 —— 比如 `state.has_escalated` 是否被正确设置、`rounds_since_todo` 的当前值 —— 有两种方式：

**方式一**：临时加 `print()`（最快）



```python
# 在 code.py 需要观察的位置加：
print(f"DEBUG rounds_since_todo={rounds_since_todo}")
```

你之前就是这样做的，效果很好。

**方式二**：扩展 Trace 钩子回调

在 `trace/trace_hooks.py` 的 `TraceHooks` 类中添加新方法，比如在 `on_post_model_call` 里额外记录你想看的变量：



```python
# trace/trace_hooks.py → TraceHooks.on_post_model_call
def on_post_model_call(self, response, messages: list):
    if response is None:
        ...
    else:
        log_model_output(self._log, response, self._step["count"])
        # 额外记录你关心的变量
        self._log.log_event("custom_debug", {
            "response_stop_reason": response.stop_reason,
            "message_count": len(messages),
        }, step=self._step["count"])
    return None
```

然后在 HTML/JSONL 输出中就能看到这个自定义事件了。

**方式三**：用 Python 标准库的 `logging`



```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

然后在需要的地方加 `logging.debug(f"var={var}")`。不过这不会写入 Trace 文件，只在控制台显示。

### 特殊情况：Agent 的错误消息你看不到

你之前遇到的"无回复"问题暴露了一个 pre-existing bug：当 API 调用失败时，错误消息用 dict 存储：



```python
# code.py L1947-1948
messages.append({"role": "assistant", "content": [
    {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
```

但 `print_turn_assistants`（L2021-2027）用 `getattr(block, "type", None)` 读取 —— 这种方法读不到 dict 的 key，只对 SDK 对象有效。所以错误消息被静默吞掉了。这是原始代码里就有的兼容性问题，不是 Trace 引入的。

------

## 三、速查：Hooks + Trace 调试工作流

### 日常使用



```python
# 1. 开启 Trace（在 main 块中，已默认配置好）
trace_logger, _trace_step = enable_trace_logging(
    register_hook, trace_dir="logs/traces", enabled=True)

# 2. 正常使用 Agent
#    → 所有交互自动记录到 logs/traces/

# 3. 退出后查看 Trace
#    → .jsonl: 用 jq/pandas 分析
#    → .html: 浏览器打开，时间线视图
```

### 临时调试某个变量



```python
# 在 code.py 的任何位置：
print(f"DEBUG: {变量名=}")

# 退出时不需要清理 —— 这只是临时调试
```

### 关闭 Trace

改一行即可：[s20_comprehensive/code.py:2057](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/s20_comprehensive/code.py#L2057)



```python
enabled=False  # Agent 行为完全不变，不产生任何 trace 文件
```

------

### 关键文件位置速查

| 内容                              | 位置                                                         |
| --------------------------------- | ------------------------------------------------------------ |
| HOOKS 定义 + register/trigger     | [s20_comprehensive/code.py:866-880](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/s20_comprehensive/code.py#L866) |
| 5 个内置 hook 注册                | [s20_comprehensive/code.py:944-948](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/s20_comprehensive/code.py#L944) |
| agent_loop 完整流程               | [s20_comprehensive/code.py:1912-2018](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/s20_comprehensive/code.py#L1912) |
| Trace 钩子回调                    | [trace/trace_hooks.py:35-98](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/trace/trace_hooks.py#L35) |
| Trace 入口（enable/disable）      | [s20_comprehensive/code.py:2055-2057](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/s20_comprehensive/code.py#L2055) |
| Trace 引擎（AgentLogger）         | [trace/agent_logger.py:533-691](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/trace/agent_logger.py#L533) |
| 事件类型定义                      | [trace/agent_logger.py:44-63](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/trace/agent_logger.py#L44) |
| print_turn_assistants（输出渲染） | [s20_comprehensive/code.py:2021-2027](vscode-webview://0imbivabbaffch90beid1mpkplpevq734udoas7bjstch1r0go95/s20_comprehensive/code.py#L2021) |
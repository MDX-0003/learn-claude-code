# Anthropic API 规范 —— s01~s20 共有的"读取 LLM 回复"逻辑

s01 到 s20 每个 `code.py` 都有一段相同的核心骨架：调 API → 读回复 → 解析 content block → 执行工具。这份文档聚焦**回复读取**这一步，说明 API 返回的数据结构、关键类型、常用成员，以及各 s0x 里读这些成员的代码写法。

---

## 1. 回复的顶层结构：`Message`

`client.messages.create()` 返回 `anthropic.types.Message`（[源码](file:///D:/Lib/python311/Lib/site-packages/anthropic/types/message.py)）：

```python
class Message(BaseModel):
    id: str                   # 消息唯一 ID，如 "msg_01A..."
    content: List[ContentBlock]  # ← 核心：所有回复内容，主要关心content
    model: Model              # 使用的模型
    role: Literal["assistant"]  # 固定 "assistant"
    stop_reason: Optional[StopReason]  # 停止原因
    stop_sequence: Optional[str]     # 触发的自定义停止序列
    type: Literal["message"]         # 固定 "message"
    usage: Usage              # token 用量
```

在所有代码里，这个Message均以`response`变量名称存在:
```
#类型 = anthropic.types.Message
response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
```

> **`Literal`** 是 `typing_extensions.Literal`，用于标注字段**只能取这几个字符串值**。比如 `role: Literal["assistant"]` 表示 `role` 永远是 `"assistant"`，不可能是别的字符串。`StopReason: TypeAlias = Literal["end_turn", "max_tokens", ...]` 则是给这组字面量起了个别名。运行时无约束，只供 pyright/mypy 做静态检查——如果代码里写了 `response.stop_reason == "finished"`，IDE 会标黄警告这不是合法值。

教程代码只用到两个字段：

| 字段 | 用途 | 首次出现 |
|---|---|---|
| `response.content` | 遍历 content block，提取文本或工具调用 | s01 |
| `response.stop_reason` | 判断是否还有 tool_use，决定循环继续还是退出 | s01 |

### 1.1 类型关系速览

下面按"容器 → 子项"的父子层级列出本文涉及的所有类型，附带教程里的实际变量名：

```
Message                              ← response = client.messages.create(...)
├── .content: List[ContentBlock]     ← response.content, 循环中变量名 block
│   ├── TextBlock                    ←  if type == "text"
│   │   └── .text: str              ←     block.text
│   └── ToolUseBlock                 ←  if type == "tool_use"
│       ├── .id: str                ←     block.id, 格式 toolu_01Xxx...
│       ├── .name: str              ←     block.name, tool的名称，如 "bash"/"read_file"
│       └── .input: Dict[str, Any]  ←     block.input, tool的参数，解包传给 handler
├── .stop_reason: StopReason        ← response.stop_reason,类型Literal[str]
│   所有可能取值: "end_turn" | "max_tokens" | "stop_sequence"
│        | "tool_use" | "pause_turn" | "refusal"
├── .role: Literal["assistant"]
├── .id: str                        ← 消息 ID (msg_01Xxx...), 教程几乎不用
└── .usage: Usage                   ← token 用量, 教程未读取
```

> **注意命名不一致**：API 返回的类是 `Message`，但教程里从未出现变量名叫 `message`——始终叫 `response`。`Message` 是 Anthropic 的 schema 类型名，`response` 是教程作者的习惯变量名，二者指向同一对象。

---

## 2. `response.content`：`List[ContentBlock]`

### 2.1 ContentBlock 是什么

[`ContentBlock`](file:///D:/Lib/python311/Lib/site-packages/anthropic/types/content_block.py) 是一个**歧视联合类型**（discriminated union），用 `type` 字段区分：

```python
ContentBlock = Union[
    TextBlock,              # type == "text"
    ThinkingBlock,          # type == "thinking"
    RedactedThinkingBlock,  # type == "redacted_thinking"
    ToolUseBlock,           # type == "tool_use"       ← 教程最常用
    ServerToolUseBlock,     # type == "server_tool_use"
    WebSearchToolResultBlock,
    WebFetchToolResultBlock,
    CodeExecutionToolResultBlock,
    BashCodeExecutionToolResultBlock,
    TextEditorCodeExecutionToolResultBlock,
    ToolSearchToolResultBlock,
    ContainerUploadBlock,
]
```

> **歧视联合**（discriminated union）和 `Literal` 是不同层面的概念，虽然长得像。
>
> `Literal` 约束的是**单个字段的值**，比如 `type: Literal["tool_use"]` 表示这个字段只能是字符串 `"tool_use"`。
>
> 歧视联合则是一个**容器类型**，它把多个类包在一起（`Union[A, B, C]`），并指定一个公共字段作为"歧视子"（discriminator）来区分具体是哪个。当代码检查这个字段的值时，类型检查器会自动**收窄**（narrow）到对应的子类型：
>
> ```python
> # block 的静态类型是 ContentBlock (Union[...])
> for block in response.content:
>     if block.type == "tool_use":        # 歧视子是 type 字段
>         # 此处 block 被收窄为 ToolUseBlock → 可以访问 .name, .input
>         handler(block.name, block.input)
>     elif block.type == "text":
>         # 此处 block 被收窄为 TextBlock → 可以访问 .text
>         print(block.text)
> ```
>
> 在 Pydantic/Anthropic SDK 的实现里，歧视联合用 `Annotated[Union[...], PropertyInfo(discriminator="type")]` 声明。
> 换言之，discriminator是type这件事，是在ContentBlock定义时决定下来的
> **每个子类内部用 `Literal` 标注自己的 discriminator 值**，联合容器用 `PropertyInfo` 告诉类型检查器> "请按 `type` 字段做收窄"。
>
> 一句话：`Literal` 管单个值能写什么；歧视联合管多个类型怎么区分。

s01~s20 的教学代码只涉及两种：**TextBlock** 和 **ToolUseBlock**。

### 2.2 TextBlock —— 模型输出的纯文本

```python
# 定义: anthropic/types/text_block.py
class TextBlock(BaseModel):
    type: Literal["text"]     # 每个子类内部用 `Literal` 标注自己的 discriminator 值
    text: str                 # 模型输出的文本内容
```

教程里的典型读法：

```python
# 遍历时跳过 tool_use，剩下的就是 TextBlock —— 打印它
for block in response.content:
    if block.type != "tool_use":
        print(block.text)    # s01:136, s02:189, s03:250, ...

# 或用 getattr 安全读取（当不确定类型时）
print(getattr(block, "text", ""))
```

### 2.3 ToolUseBlock —— 模型发起的工具调用

```python
# 定义: anthropic/types/tool_use_block.py
class ToolUseBlock(BaseModel):
    id: str                   # 工具调用唯一 ID，服务端生成
    name: str                 # 工具名，如 "bash", "read_file"
    input: Dict[str, object]  # 参数 JSON，如 {"command": "ls -la"}
    type: Literal["tool_use"]
    caller: Optional[Caller] = None  # 调用来源（教学版忽略）
```

教程里的典型读法——按 `type == "tool_use"` 收窄类型后，直接访问 `id` / `name` / `input`：

```python
for block in response.content:
    if block.type != "tool_use":
        continue

    # block 在此处被 pyright/mypy 收窄为 ToolUseBlock
    print(f"> {block.name}")                        # 工具名
    handler = TOOL_HANDLERS.get(block.name)         # 按名查 handler
    output = handler(**block.input)                 # 解包参数调用
    results.append({
        "type": "tool_result",
        "tool_use_id": block.id,                    # 回传 ID，匹配请求
        "content": output,
    })
```

### 2.4 `id` 字段的格式

`block.id` 由 Anthropic API **服务端生成**，SDK 不做任何客户端赋值。真实格式：

```
toolu_01D7FLrfh4GYq7yT1ULFeyMV
```

固定前缀 `toolu_` + 随机字符串。教学代码中出现的 `tool_001`、`tool_004` 等是**教学简化**，实际 API 不会返回自增编号。

---

## 3. `response.stop_reason`：`StopReason`

StopReason ： 引擎停止生成 token 了，原因是什么
"stop" 的对象就是 token generation stream（token 生成流）

```python
# 定义: anthropic/types/stop_reason.py
StopReason = Literal[
    "end_turn",      # 模型自然结束，没有 tool_use
    "max_tokens",    # 达到 max_tokens 上限
    "stop_sequence", # 触发了自定义 stop_sequences
    "tool_use",      # 模型发起了一个或多个工具调用
    "pause_turn",    # 长轮次暂停（教学版不涉及）
    "refusal",       # 危险操作服务端直接中断（教学版不涉及）
]
```

教程里的典型用法：

```python
response = client.messages.create(...)

# 追加整条 assistant 消息到历史
messages.append({"role": "assistant", "content": response.content})

# 没有 tool_use → 结束循环，返回给用户
if response.stop_reason != "tool_use":
    return
# 否则继续处理 content 里的 tool_use block
```

`s20` 额外处理了 `max_tokens`——写到一半被截断时主动续写：

```python
if response.stop_reason == "max_tokens":
    # 追加 assistant 消息后立刻再发请求，让模型接着写
    messages.append({"role": "assistant", "content": response.content})
    continue  # 回到循环开头，再调一次 API
```

---

## 4. 共有的 agent loop 骨架

s01 到 s20 所有 `code.py` 的 `agent_loop` 都遵循同一个模板。以下是**去掉了各 s0x 特有扩展**后的最小骨架：

```python
def agent_loop(messages: list):
    while True:
        # ── ① 调 API ──
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        # ── ② 把 assistant 回复整个加入历史 ──
        messages.append({"role": "assistant", "content": response.content})

        # ── ③ 判断是否继续 ──
        if response.stop_reason != "tool_use":
            return   # 结束，上层代码打印最后的 text

        # ── ④ 遍历 content block，逐个执行工具 ──
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue   # 跳过 TextBlock

            print(f"> {block.name}")

            # 各 s0x 在这里插入自己的逻辑：
            #   s03: check_permission(block)
            #   s04: trigger_hooks("PreToolUse", block)
            #   s13: should_run_background(block.name, block.input)
            #   ...

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        # ── ⑤ 工具结果作为 user 消息加入历史，继续下一轮 ──
        messages.append({"role": "user", "content": results})
```

---

## 5. 各 s0x 读取 block 成员的代码速查

只列出**与 response 解析直接相关**的那几行，忽略各 s0x 的特有逻辑。

### 5.1 基础读法（s01 ~ s03）

```python
# s01_agent_loop/code.py:96-108  —— 最原始形态
if response.stop_reason != "tool_use":
    return
for block in response.content:
    if block.type == "tool_use":
        output = run_bash(block.input["command"])    # 只认 command 字段
        results.append({"type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output})
```

```python
# s02_tool_use/code.py:158-168  —— 引入多工具，用 block.name 查 handler
if response.stop_reason != "tool_use":
    return
for block in response.content:
    if block.type == "tool_use":
        handler = TOOL_HANDLERS.get(block.name)          # block.name 取值：bash/read_file/...
        output = handler(**block.input)                  # block.input 解包传参
        results.append({"type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output})
```

```python
# s03_permission/code.py:210-229  —— 在 handler 调用前插入 check_permission
if response.stop_reason != "tool_use":
    return
for block in response.content:
    if block.type != "tool_use":
        continue
    if not check_permission(block):          # ← 读取 block.name / block.input
        results.append({"type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Permission denied."})
        continue
    handler = TOOL_HANDLERS.get(block.name)
    output = handler(**block.input)
    results.append({"type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output})
```

### 5.2 Hooks 模式（s04 ~ s05）

```python
# s04_hooks/code.py:244-270  —— PreToolUse hook 读取 block 判断权限
if response.stop_reason != "tool_use":
    return
for block in response.content:
    if block.type != "tool_use":
        continue
    decision = trigger_hooks("PreToolUse", block)
    # hook 内部读取：block.name, block.input.get("command", ""),
    #               block.input.get("path", "")
    if decision == "blocked":
        results.append({"type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Permission denied."})
        continue
    handler = TOOL_HANDLERS.get(block.name)
    output = handler(**block.input)
    results.append({"type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output})
```

### 5.3 子 Agent 读法（s06 ~ s08）

```python
# s06_subagent/code.py:199-215  —— 子 Agent 内部循环，和主循环结构一致
if response.stop_reason != "tool_use":
    return
for block in response.content:
    if block.type == "tool_use":
        handler = SUB_HANDLERS.get(block.name)
        output = handler(**block.input)
        results.append({"type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output})
```

```python
# s08_context_compact/code.py:213-228  —— 工具调用前做 compact 检查
if response.stop_reason != "tool_use":
    return
for block in response.content:
    if block.type == "tool_use":
        if block.name == "compact":
            # 特殊工具用自己的 id 匹配
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": do_compact(messages)})
            continue
        handler = SUB_HANDLERS.get(block.name)
        output = handler(**block.input)
        results.append({"type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output})
```

### 5.4 结束轮次读取 text（所有 s0x 通用）

```python
# s01:136, s02:189, s03:250, ...  —— agent_loop 返回后打印模型最终文本
for block in history[-1]["content"]:
    if getattr(block, "type", None) == "text":
        print(block.text)
```

这里用 `getattr` 而非直接 `.text`，因为 `history[-1]["content"]` 是 `List[ContentBlock]`（JSON 序列化后丢失了类型信息），需要安全访问。

### 5.5 extract_text 工具函数（s06+）

```python
# s06_subagent/code.py:139, s09_memory/code.py:404,
# s20_comprehensive/code.py:996
def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    ).strip()
```

### 5.6 has_tool_use（s20）

```python
# s20_comprehensive/code.py:1005-1009
# 不使用 stop_reason，直接用 content 内容判断
def has_tool_use(content) -> bool:
    return any(getattr(block, "type", None) == "tool_use"
               for block in content)
```

原因注释在源码里：*"Do not rely on stop_reason alone; the concrete tool_use block is the continuation signal used by the loop."*

### 5.7 max_tokens 续写（s11 / s20）

```python
# s11_error_recovery/code.py:301, s20_comprehensive/code.py:1952
response = client.messages.create(...)
if response.stop_reason == "max_tokens":
    # 回复被截断，追加 assistant 消息后立刻再请求
    messages.append({"role": "assistant", "content": response.content})
    continue   # 回到 while True 顶部，再调 API
```

### 5.8 后台执行判断（s13 ~ s17）

```python
# s13_background_tasks/code.py:428-444
for block in response.content:
    if block.type != "tool_use":
        continue
    if should_run_background(block.name, block.input):
        # 读 block.input 判断是否后台任务
        results.append({"type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Running in background..."})
        continue
    # ... 同步执行
```

---

## 6. 类型速查卡

### 6.1 回复层

```
response: Message
  .content: List[ContentBlock]     ← 回复正文，每条是一个 block
  .stop_reason: "end_turn" | "max_tokens" | "stop_sequence" |
                 "tool_use" | "pause_turn" | "refusal"
  .id: str                         ← 消息 ID（msg_01A...），教程很少用
```

### 6.2 Content block 层

```
block: ContentBlock (= Union[TextBlock, ToolUseBlock, ...])

当 block.type == "text" → TextBlock:
  .text: str                       ← 模型给出的文本

当 block.type == "tool_use" → ToolUseBlock:
  .id: str                         ← 服务端生成，格式 toolu_01Xxx...
  .name: str                       ← 工具名，对应 TOOLS 定义里的 name
  .input: Dict[str, object]        ← 工具参数，直接 **block.input 解包给 handler
```

### 6.3 工具结果回传

```python
# 构造 tool_result 时必须用 block.id 做 tool_use_id，否则 API 会报错
{
    "type": "tool_result",
    "tool_use_id": block.id,       # 必须精确匹配
    "content": "..."               # 字符串
}
```

---

## 7. SDK 源码位置速查

| 类型 | 路径 |
|---|---|
| `Message` | `site-packages/anthropic/types/message.py` |
| `ContentBlock` | `site-packages/anthropic/types/content_block.py` |
| `TextBlock` | `site-packages/anthropic/types/text_block.py` |
| `ToolUseBlock` | `site-packages/anthropic/types/tool_use_block.py` |
| `StopReason` | `site-packages/anthropic/types/stop_reason.py` |

所有这些文件均由 `Stainless` 从 OpenAPI spec 自动生成，顶部有注释 `# File generated from our OpenAPI spec by Stainless`。

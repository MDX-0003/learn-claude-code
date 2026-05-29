# Doc C：上下文管线 —— LLM 调用前发生了什么

## 读者定位

读者已理解 agent_loop 的执行流（Doc A）和工具调度（Doc B），现在需要理解"每次 LLM 调用前，messages 和 system prompt 经历了怎样的处理管线"。

---

## 一、管线全景

每轮 agent_loop 迭代中，LLM 调用前有一条 5 步管线：

```
messages (原始)
  │
  ├─ 阶段 ① tool_result_budget()     ← 超大的工具结果 → 持久化到文件，替换为预览
  ├─ 阶段 ② snip_compact()           ← 消息条数过多 → 截断中间部分
  ├─ 阶段 ③ micro_compact()          ← 旧工具结果过多 → 清除超过 3 轮的
  ├─ 阶段 ④ compact_history()        ← 估算 tokens 超限 → 调 LLM 做摘要压缩
  │
  ▼
messages (压缩后)  +  system_prompt (动态组装)
  │
  └─ 阶段 ⑤ call_llm()               ← 最终调用
```

这 5 步在 `prepare_context()`（L1872-1879）和 `call_llm()`（L1899-1909）中执行：

```python
# agent_loop L1934-1940
prepare_context(messages)   # ①→④
context = update_context(context, messages)
tools, handlers = assemble_tool_pool()

response = call_llm(messages, context, tools, state, max_tokens)  # ⑤
```

---

## 二、阶段 ①：tool_result_budget() —— 大结果防爆

`s20_comprehensive/code.py:1079-1100`

**要解决的问题**：工具（尤其是 `bash`）可能返回几十万字符的输出。全部塞进 messages 会导致 token 消耗爆炸。

**怎么做**：检查最新一条 user 消息中所有 tool_result 的总大小。如果超过 200KB，从最大的结果开始逐个替换为文件引用 + 预览。

```python
def tool_result_budget(messages: list, max_bytes: int = 200_000) -> list:
    last = messages[-1]
    blocks = [(i, b) for i, b in enumerate(content)
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    if total <= max_bytes:
        return messages  # 不超标，直接返回

    for _, block in sorted(blocks, key=len, reverse=True):  # 从最大的开始
        text = str(block.get("content", ""))
        block["content"] = persist_large_output(              # → 写入 .task_outputs/
            block.get("tool_use_id", "unknown"), text)
        ...
```

`persist_large_output()`（L1068-1076）将完整输出写入 `.task_outputs/tool-results/{tool_use_id}.txt`，返回一个摘要标记：

```python
def persist_large_output(tool_use_id: str, output: str) -> str:
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    path.write_text(output)
    return (f"<persisted-output>\nFull output: {path}\n"
            f"Preview:\n{output[:2000]}\n</persisted-output>")
```

LLM 看到 `<persisted-output>` 标记就知道完整内容在文件中，可以用 `read_file` 按需读取。

---

## 三、阶段 ②：snip_compact() —— 消息条数防爆

`s20_comprehensive/code.py:1103-1110`

**要解决的问题**：长时间对话后 messages 可能有上百条。全部保留超出了很多模型的上下文窗口。

**怎么做**：保留头 3 条和尾 47 条（默认 `max_messages=50`），中间替换为一条标记消息。

```python
def snip_compact(messages: list, max_messages: int = 50) -> list:
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    return (messages[:keep_head]
            + [{"role": "user", "content": f"[snipped {snipped} messages]"}]
            + messages[-keep_tail:])
```

头 3 条保留是因为它们通常包含初始任务描述和身份设定；尾 47 条是最近的上下文。

---

## 四、阶段 ③：micro_compact() —— 旧工具结果清理

`s20_comprehensive/code.py:1113-1120`

**要解决的问题**：即使消息条数没超标，大量旧的 tool_result 也占用了不必要的 token。LLM 通常只关心最近几轮的工具输出。

**怎么做**：找到所有历史 tool_result，只保留最近 3 个，更早的替换为标记文本。

```python
KEEP_RECENT_TOOL_RESULTS = 3

def micro_compact(messages: list) -> list:
    tool_results = collect_tool_results(messages)  # → [(msg_idx, block_idx, block), ...]
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages

    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(str(block.get("content", ""))) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages
```

被清除的 tool_result 内容不会丢失 —— 如果需要，LLM 可以重新执行工具。

---

## 五、阶段 ④：compact_history() —— 灾难性压缩

`s20_comprehensive/code.py:1144-1148`

**要解决的问题**：前三步仍然不够，`estimate_size()` 估算的总 token 数超过 `CONTEXT_LIMIT`（50000）。此时需要"核选项"——调 LLM 做摘要压缩。

```python
CONTEXT_LIMIT = 50000

# 在 prepare_context 中（L1877）：
if estimate_size(messages) > CONTEXT_LIMIT:
    messages[:] = compact_history(messages)

def estimate_size(messages: list) -> int:
    return len(json.dumps(messages, default=str))     # 序列化后按字符数估算
```

### 5.1 compact_history 三步

```python
def compact_history(messages: list) -> list:
    transcript = write_transcript(messages)  # ① 完整对话存档到 .transcripts/
    summary = summarize_history(messages)     # ② 调 LLM 生成摘要
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]  # ③ 只保留摘要
```

存档格式是 JSONL（L1123-1129），每条消息一行。摘要 LLM 调用（L1132-1141）用独立的 `client.messages.create`，max_tokens=2000，只做总结不做工具调用。

压缩后，messages 被替换为**仅一条**包含完整摘要的 user 消息。这意味着所有历史细节都丢失了，LLM 只能从摘要中推断上下文。

### 5.2 reactive_compact() —— 被动压缩

当 API 返回"prompt too long"错误时触发（L1151-1159）。与主动压缩的区别：
- 主动压缩发生在 LLM 调用**前**（`estimate_size > CONTEXT_LIMIT`）
- 被动压缩发生在 LLM 调用**失败后**（`is_prompt_too_long_error(e)`）

被动压缩会保留最近 5 条消息，在摘要后追加 `*messages[-5:]`，给 LLM 保留一些即时上下文。

---

## 六、System Prompt：动态组装

`s20_comprehensive/code.py:343-374`

System prompt 不是写死的字符串，而是每轮 LLM 调用前根据当前 context 动态构建。

### 6.1 模板（L345-357）

```python
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, edit_file, ...",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}
```

### 6.2 组装过程（L360-374）

```python
def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    sections.append("Skills catalog:\n" + list_skills()
                    + "\nUse load_skill(name) when a skill is relevant.")

    if context.get("memories"):                           # 有 memory 时注入
        sections.append(f"Relevant memories:\n{context['memories']}")

    mcp_names = list(mcp_clients.keys())
    if mcp_names:                                          # 有 MCP 连接时注入
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")

    return "\n\n".join(sections)
```

动态部分：
- `Current time`：每轮更新，让 LLM 知道当前时间
- `Skills catalog`：从 `SKILL_REGISTRY` 动态生成
- `memories`：从 `MEMORY_DIR/MEMORY.md` 读取（L1857-1858）
- `mcp_names`：从已连接的 MCP 客户端列表生成

### 6.3 context dict 的生命周期

`context` 在 main 中初始化（L2062），每轮 agent_loop 返回后通过 `update_context()` 刷新（L2078）：

```python
def update_context(context: dict, messages: list) -> dict:
    memories = ""
    if MEMORY_INDEX.exists():
        memories = MEMORY_INDEX.read_text()[:2000]
    return {
        "memories": memories,
        "connected_mcp": list(mcp_clients.keys()),
        "active_teammates": list(active_teammates.keys()),
    }
```

三个维度每次刷新：memory 文件内容、MCP 连接状态、当前活跃的队友列表。

---

## 七、messages 的结构

理解 messages 的数据结构对于理解压缩管线至关重要。messages 是 Anthropic Messages API 格式：

```python
[
    {"role": "user", "content": "列出文件"},                    # 用户输入
    {"role": "assistant", "content": [TextBlock, ToolUseBlock]},  # LLM 响应
    {"role": "user", "content": [                               # 工具结果（由 agent_loop 构造）
        {"type": "tool_result", "tool_use_id": "...", "content": "file1.py\nfile2.py"}
    ]},
    {"role": "assistant", "content": [TextBlock]},              # LLM 最终文本回复
]
```

关键差异：
- `assistant` 消息的 `content` 是 SDK 对象列表（`TextBlock`, `ThinkingBlock`, `ToolUseBlock`）
- `user` 消息的 `tool_result` 是普通 dict（由 agent_loop 用 `{"type": "tool_result", ...}` 构造）

这就是为什么 `print_turn_assistants` 用 `getattr(block, "type", None)` 而压缩管线用 `isinstance(block, dict) and block.get("type")` —— 因为它们的操作对象不同。

---

## 速查

| 内容 | 代码位置 | 触发条件 |
|------|---------|---------|
| prepare_context() | L1872-1879 | 每轮 agent_loop |
| tool_result_budget() | L1079-1100 | 最近一条 user 消息的 tool_result 总大小 > 200KB |
| snip_compact() | L1103-1110 | messages 条数 > 50 |
| micro_compact() | L1113-1120 | 历史 tool_result > 3 个 |
| compact_history() | L1144-1148 | estimate_size > CONTEXT_LIMIT (50000) |
| reactive_compact() | L1151-1159 | API 返回 prompt-too-long 错误 |
| write_transcript() | L1123-1129 | compact_history / reactive_compact 时 |
| summarize_history() | L1132-1141 | compact_history / reactive_compact 时 |
| assemble_system_prompt() | L360-374 | 每轮 call_llm() |
| update_context() | L1855-1863 | 每轮 agent_loop 返回后 |
| estimate_size() | L1052-1053 | prepare_context 中 |
| collect_tool_results() | L1056-1065 | micro_compact 中 |
| persist_large_output() | L1068-1076 | tool_result_budget 中 |
| PROMPT_SECTIONS 模板 | L345-357 | 模块加载时 |
| CONTEXT_LIMIT 常量 | L49 | — |
| KEEP_RECENT_TOOL_RESULTS 常量 | L50 | — |
| PERSIST_THRESHOLD 常量 | L51 | — |

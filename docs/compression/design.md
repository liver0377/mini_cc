# 上下文压缩设计文档

## 概述

上下文压缩（Context Compression）解决的是长对话中上下文窗口不够的问题。当对话轮次增多、上下文 token 数接近模型限制时，用 LLM 生成的结构化摘要替代完整的对话历史，让模型在有限的上下文窗口内仍能保持对全局的理解。

压缩本质上是一种**会话级的短期记忆**——生命周期仅限当前会话，会话结束即废弃。与中期记忆系统（`docs/memory/`）不同，压缩不跨会话持久化。

系统支持**三种压缩场景**，覆盖主动预防、被动恢复和用户主动触发：

```
┌─────────────────────────────────────────────────┐
│  场景 1: 自动压缩 (Auto Compact)                 │
│                                                  │
│  _query_loop 每轮开始前检测 token 超阈值           │
│  → 在 API 调用之前执行                            │
│  → yield CompactOccurred 通知 UI                  │
│  → 成功后重置 has_attempted_reactive_compact      │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  场景 2: 反应式压缩 (Reactive Compact)            │
│                                                  │
│  API 返回 413 / context_length_exceeded           │
│  → 在错误恢复阶段执行                             │
│  → 仅尝试一次（has_attempted_reactive_compact）   │
│  → 失败后不再重试，异常继续向上抛出                │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  场景 3: 手动压缩 (/compact 命令)                 │
│                                                  │
│  用户输入 /compact                                │
│  → UI 层直接调用 compress_messages()              │
│  → 替换当前 state.messages                        │
└─────────────────────────────────────────────────┘
```

## Token 计数

### 实现方式

使用 `tiktoken` 库进行精确 token 计数。编码器选择策略：

```
模型名 → tiktoken.encoding_for_model(model)
         ↓ 失败（非 OpenAI 模型）
         → tiktoken.get_encoding("cl100k_base")  # GPT-4 通用编码器兜底
```

`cl100k_base` 对大多数 OpenAI 兼容模型（GLM、DeepSeek、Qwen 等）的估算精度足够用于阈值判断。

### 计数范围

```python
def estimate_tokens(messages: list[Message], model: str) -> int:
    encoding = _get_encoding(model)
    total = 0
    for msg in messages:
        total += 4  # 每条消息的格式开销 (<role>\n...<end>)
        if msg.content:
            total += len(encoding.encode(msg.content))
        for tc in msg.tool_calls:
            total += len(encoding.encode(tc.arguments))
        if msg.name:
            total += len(encoding.encode(msg.name))
    return total
```

### 控制参数

| 参数 | 来源 | 默认值 | 说明 |
|------|------|--------|------|
| `AUTO_COMPACT_THRESHOLD` | 环境变量 `AUTO_COMPACT_THRESHOLD` | `80000` | 自动压缩的 token 阈值 |
| 编码器 | 模型名自动选择 | `cl100k_base` | tiktoken 编码器 |

## 核心流程：compress_messages()

三种场景最终都调用同一个核心函数：

```python
async def compress_messages(
    messages: list[Message],
    stream_fn: StreamFn,
    existing_summary: str | None = None,
) -> str:
```

### 执行步骤

1. 保留 `messages[0]`（system message）
2. 将所有非 system 消息格式化为文本（角色标签 + 内容，tool_call 结果摘要）
3. 构建 LLM prompt：`COMPRESSION_SYSTEM_PROMPT` + 格式化的对话文本
4. 通过 `stream_fn`（复用项目的 LLMProvider）调用 LLM 生成摘要
5. 返回摘要文本

### 压缩后的消息替换

压缩发生后，`state.messages` 被替换为：

```
[0] system message（保持不变）
[1] user message: "以下是之前对话的摘要：\n{summary}"
```

后续对话正常追加到 `state.messages`。当再次超过阈值时，摘要 + 新消息一起被再次压缩。

## 场景 1：自动压缩 (Auto Compact)

### 触发时机

`_query_loop` **每轮开始前**，在 API 调用之前检查。

### 流程

```
_query_loop(state):
    has_attempted_reactive = False
    while True:
        if interrupted: break

        # === Phase 1: 自动压缩 ===
        token_count = estimate_tokens(state.messages)
        if token_count >= AUTO_COMPACT_THRESHOLD:
            summary = await compress_messages(state.messages, stream_fn)
            _replace_with_summary(state, summary)
            yield CompactOccurred(reason="auto")
            has_attempted_reactive = False  # 压缩后重置

        # 正常流式调用
        ...
```

### 设计考量

- **在 API 调用前执行**：主动预防，避免浪费一次 API 调用
- **每次压缩后重置 reactive 标记**：压缩后 token 数已降低，允许新的 reactive 尝试

## 场景 2：反应式压缩 (Reactive Compact)

### 触发时机

`_query_loop` 中 `stream_fn` 抛出 `ContextLengthExceeded` 异常时。

### 错误检测链路

```
OpenAI API 返回 400 BadRequest
  → 错误码 context_length_exceeded
  → openai.BadRequestError
  → providers/openai.py 捕获，转换为自定义 ContextLengthExceeded
  → query_engine/engine.py 捕获，执行反应式压缩
```

### 流程

```
_query_loop(state):
    ...
    # === Phase 2: 带反应式压缩的流式调用 ===
    try:
        async for event in stream_fn(state.messages, schemas):
            yield event
            turn_events.append(event)
    except ContextLengthExceeded:
        if has_attempted_reactive:
            raise  # 已尝试过，不再重试
        has_attempted_reactive = True
        summary = await compress_messages(state.messages, stream_fn)
        _replace_with_summary(state, summary)
        yield CompactOccurred(reason="reactive")
        continue  # 重试当前 turn
```

### 设计考量

- **仅尝试一次**：`has_attempted_reactive_compact` 防止无限重试循环（压缩后仍然超限 → 无限压缩）
- **压缩失败则异常上抛**：让 UI 层显示错误信息
- **`continue` 重试**：压缩成功后重新进入循环，执行 API 调用

## 场景 3：手动压缩 (/compact 命令)

### 触发时机

用户在输入框中输入 `/compact`。

### TUI 模式

`chat_screen.py` 的 `_send_message()` 中拦截：

```
_send_message(text):
    if text == "/compact":
        summary = await compress_messages(state.messages, engine.stream_fn)
        _replace_with_summary(state, summary)
        await chat.add_system_message("对话已压缩")
        return
```

### CLI 模式

`cli.py` 的 chat 循环中拦截：

```
chat loop:
    text = user_input.strip()
    if text == "/compact":
        summary = await compress_messages(state.messages, engine.stream_fn)
        _replace_with_summary(state, summary)
        rprint("[dim]对话已压缩[/]")
        continue
```

## 新增事件类型

### CompactOccurred

```python
@dataclass
class CompactOccurred:
    reason: str  # "auto" | "reactive" | "manual"
```

### 在 Event union 中的位置

```python
Event = (
    TextDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | ToolResultEvent
    | CompactOccurred       # ← 新增
    | AgentStartEvent
    | AgentTextDeltaEvent
    | AgentToolCallEvent
    | AgentToolResultEvent
    | AgentCompletionNotificationEvent
)
```

### UI 渲染

`CompactOccurred` 在 TUI 和 CLI 中渲染为系统提示消息：

- auto: `[dim]（上下文已自动压缩）[/]`
- reactive: `[yellow]（上下文超出限制，已自动压缩后重试）[/]`
- manual: `[dim]（对话已手动压缩）[/]`

## 新增异常类型

### ContextLengthExceeded

```python
class ContextLengthExceeded(Exception):
    pass
```

在 `providers/openai.py` 中捕获 `openai.BadRequestError`，检测错误码 `context_length_exceeded`，转换为 `ContextLengthExceeded`。

## 消息替换逻辑

```python
def _replace_with_summary(state: QueryState, summary: str) -> None:
    system_msg = state.messages[0] if state.messages and state.messages[0].role == Role.SYSTEM else None
    state.messages.clear()
    if system_msg:
        state.messages.append(system_msg)
    state.messages.append(Message(
        role=Role.USER,
        content=f"以下是之前对话的摘要：\n\n{summary}",
    ))
```

## 模块结构

```
src/mini_cc/compression/
├── __init__.py              # 公共导出
├── compressor.py            # 核心压缩逻辑
│   - ContextLengthExceeded
│   - estimate_tokens(messages, model) -> int
│   - compress_messages(messages, stream_fn, existing_summary) -> str
│   - _replace_with_summary(state, summary)
│   - _get_encoding(model) -> tiktoken.Encoding
│
└── prompts.py               # 压缩用 prompt 模板
    - COMPRESSION_SYSTEM_PROMPT
```

## 集成点

| 位置 | 修改 |
|------|------|
| `pyproject.toml` | 添加 `tiktoken` 依赖 |
| `query_engine/state.py` | 新增 `CompactOccurred` 到 Event union |
| `query_engine/engine.py` | `_query_loop()` 中加入 Phase 1 自动压缩 + Phase 2 反应式压缩 |
| `providers/openai.py` | `stream()` 中捕获 `BadRequestError`，转换为 `ContextLengthExceeded` |
| `tui/screens/chat_screen.py` | `_send_message()` 拦截 `/compact`；`_handle_event()` 渲染 `CompactOccurred` |
| `cli.py` | chat 循环中拦截 `/compact` |
| `repl.py` | `create_engine()` 中将 `stream_fn` 和 `model` 暴露给压缩模块 |

## 关键设计决策

1. **tiktoken + cl100k_base 兜底**：精确计数优先，非 OpenAI 模型使用通用编码器兜底，兼容 GLM、DeepSeek 等模型。

2. **环境变量配置阈值**：`AUTO_COMPACT_THRESHOLD` 可通过环境变量自定义，默认 80000。

3. **三种场景共用 compress_messages()**：统一核心逻辑，不同场景只决定触发时机和后续行为。

4. **单条 user 摘要**：压缩后用一条 user message 携带摘要，结构简洁。下次压缩时摘要 + 新消息一起被再次压缩。

5. **反应式压缩仅一次**：防止压缩后仍超限导致的无限重试循环。

6. **主动压缩在 API 调用前**：不浪费一次注定失败的 API 调用。

7. **复用 LLMProvider**：压缩调用直接用项目的 `StreamFn`，不引入新的 API 客户端。

## 与其他模块的交互

```
compression/
  ├── 被调用 ← query_engine/engine.py     自动压缩 + 反应式压缩
  ├── 被调用 ← tui/screens/chat_screen.py  手动压缩 (/compact)
  ├── 被调用 ← cli.py                      手动压缩 (/compact)
  ├── 调用 → providers/base.py            压缩需要 LLM 调用（通过 StreamFn）
  └── 被测试 ← tests/compression/
      └── test_compressor.py
```

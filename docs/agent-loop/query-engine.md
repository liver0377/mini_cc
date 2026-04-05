# Query Engine 设计

## 概述

Query Engine 是 Mini Claude Code 的核心组件，负责驱动 LLM 的多轮"思考-行动"循环。它接收已组装好的对话状态（包含 system prompt），流式调用 LLM，调度工具执行，并将事件实时推送给消费方（TUI / REPL）。

QueryEngine 是一个**纯编排器**——它不负责 system prompt 拼装、斜杠命令解析等 UI 层职责。所有外部能力通过 `StreamFn`（流式回调）和 `ToolUseContext`（工具使用上下文）注入，实现完全解耦。

### 职责划分

| 职责 | 所在位置 | 说明 |
|------|---------|------|
| System prompt 拼装 | `context/system_prompt.py` | `SystemPromptBuilder.build()` 组装静态 prompt + 环境信息 + AGENTS.md + 记忆 |
| System prompt 注入 | 调用层（TUI `ChatScreen`、CLI `chat()`、`AgentManager`） | 创建 `QueryState` 时作为 `messages[0]` 注入 |
| 斜杠命令解析 | TUI: `ChatScreen._send_message()` / CLI: `cli.py` | `/help`、`/clear`、`/mode`、`/compact`、`/agents`、`/exit` |
| Agent 循环驱动 | `query_engine/engine.py` | `QueryEngine._query_loop()` — 本模块 |

## 整体架构

```
用户输入 "请修复这个 bug"（斜杠命令已由 TUI/CLI 处理）
       │
       ▼
┌─ 调用层（TUI ChatScreen / CLI chat / AgentManager）─────────┐
│  1. SystemPromptBuilder.build() → 组装 system prompt         │
│  2. QueryState(messages=[system_msg, ...])                    │
│  3. engine.submit_message(prompt, state)                     │
└──────────────────────────────────────────────────────────────┘
       │
       ▼
┌─ QueryEngine ───────────────────────────────────────────────┐
│                                                              │
│  submit_message(prompt, state) → AsyncGenerator[Event]      │
│    │                                                         │
│    ├── 追加用户消息到 state.messages                          │
│    └── 进入 _query_loop(state)                               │
│          │                                                   │
│          ▼  (思考-行动循环)                                   │
│    ┌─ _query_loop() ────────────────────────────────────┐    │
│    │  while True:                                       │    │
│    │    if ctx.is_interrupted: break                    │    │
│    │    排空 completion_queue / agent_event_queue       │    │
│    │    if should_auto_compact: 自动压缩 → yield Compact │    │
│    │    schemas = ctx.get_tool_schemas()                │    │
│    │    async for event in stream_fn(msgs, schemas):    │    │
│    │      yield event  ← 文本/工具事件实时推送            │    │
│    │      收集 turn_events                              │    │
│    │    (若 ContextLengthExceeded: 被动压缩 → continue)  │    │
│    │    tool_calls = collect_tool_calls(events)         │    │
│    │    if 无 tool_calls:                               │    │
│    │      break  ← 正常结束                              │    │
│    │    权限检查 → 过滤 allowed tool_calls               │    │
│    │    async for result in ctx.execute(allowed):       │    │
│    │      排空 agent_event_queue                        │    │
│    │      yield result                                  │    │
│    │    追加 assistant + tool messages 到 state          │    │
│    │    记录 TurnRecord（耗时/工具摘要）                  │    │
│    │    await post_turn_hook(state)                     │    │
│    │                                                    │    │
│    │  退出后排空剩余 completion / agent 事件              │    │
│    └────────────────────────────────────────────────────┘    │
│                                                              │
│  依赖注入：                                                   │
│    stream_fn: StreamFn       ← 外部注入的 LLM 流式回调       │
│    tool_use_ctx: ToolUseContext ← 工具上下文（schemas + 执行  │
│                                   + 权限 + 中断 + 追踪）      │
│    completion_queue          ← 子 agent 完成通知队列          │
│    agent_event_queue         ← 子 agent 实时事件队列          │
│    post_turn_hook            ← 每轮结束后的回调（memory 等）  │
└──────────────────────────────────────────────────────────────┘
```

## 核心类型（state.py）

### Message

```python
class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # JSON string


class Message(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # 仅用于 role=tool 的消息
    name: str | None = None          # 仅用于 role=tool 的消息
```

### Event

LLM 流式响应中的每个 chunk 被转换为以下 Event 类型：

| Event | 含义 | 何时产生 |
|-------|------|----------|
| `TextDelta` | 模型输出的文本片段 | `delta.content` 有值 |
| `ToolCallStart` | 工具调用开始，携带 id 和工具名 | `delta.tool_calls[i].function.name` 有值 |
| `ToolCallDelta` | 工具参数的增量 JSON 片段 | `delta.tool_calls[i].function.arguments` 有值 |
| `ToolCallEnd` | 该工具调用的流式输出结束 | `choice.finish_reason == "tool_calls"` |
| `ToolResultEvent` | 工具执行完毕后的结果 | 由 `StreamingToolExecutor.run` 产生 |
| `CompactOccurred` | 上下文压缩已发生 | `_query_loop` 中自动/被动压缩后 |
| `AgentStartEvent` | 子 agent 开始执行 | 后台 readonly agent 启动时 |
| `AgentTextDeltaEvent` | 子 agent 的文本输出片段 | 子 agent 流式输出中 |
| `AgentToolCallEvent` | 子 agent 调用了工具 | 子 agent 的工具执行前 |
| `AgentToolResultEvent` | 子 agent 工具执行结果 | 子 agent 的工具执行后 |
| `AgentCompletionNotificationEvent` | 子 agent 完成通知 | 从 `completion_queue` 中排空时 |

```python
@dataclass
class TextDelta:
    content: str

@dataclass
class ToolCallStart:
    tool_call_id: str
    name: str

@dataclass
class ToolCallDelta:
    tool_call_id: str
    arguments_json_delta: str

@dataclass
class ToolCallEnd:
    tool_call_id: str

@dataclass
class ToolResultEvent:
    tool_call_id: str
    name: str
    output: str
    success: bool

@dataclass
class CompactOccurred:
    reason: str  # "auto" | "reactive"

@dataclass
class AgentStartEvent:
    agent_id: str
    task_id: int
    prompt: str

@dataclass
class AgentTextDeltaEvent:
    agent_id: str
    content: str

@dataclass
class AgentToolCallEvent:
    agent_id: str
    tool_name: str

@dataclass
class AgentToolResultEvent:
    agent_id: str
    tool_name: str
    success: bool
    output_preview: str

@dataclass
class AgentCompletionNotificationEvent:
    agent_id: str
    task_id: int
    success: bool
    output: str
    output_path: str

Event = (
    TextDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | ToolResultEvent
    | CompactOccurred
    | AgentStartEvent
    | AgentTextDeltaEvent
    | AgentToolCallEvent
    | AgentToolResultEvent
    | AgentCompletionNotificationEvent
)
```

#### 流式 chunk 到 Event 的映射

Provider（如 OpenAIProvider）通过检查 streaming chunk 的字段来判断类型：

```
chunk.choices[0].delta.content 有值            → TextDelta
chunk.choices[0].delta.tool_calls[i] 有值：
  ├─ .function.name 有值                       → ToolCallStart
  └─ .function.arguments 有值                  → ToolCallDelta
chunk.choices[0].finish_reason == "tool_calls" → ToolCallEnd
```

#### 示例：一次 file_read 调用的完整事件流

```
TextDelta      → content="让我读取这个文件。"
ToolCallStart  → tool_call_id="tc_1", name="file_read"
ToolCallDelta  → tool_call_id="tc_1", arguments_json_delta='{"file'
ToolCallDelta  → tool_call_id="tc_1", arguments_json_delta='_path":"/tmp/a"}'
ToolCallEnd    → tool_call_id="tc_1"
               ↓ collect_tool_calls() 拼接为
               ToolCall(id="tc_1", name="file_read", arguments='{"file_path":"/tmp/a"}')
               ↓ ToolUseContext.execute()
ToolResultEvent → tool_call_id="tc_1", name="file_read", output="file content...", success=True
```

### State & Tracking

```python
class QueryState(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    turn_count: int = 0

@dataclass
class QueryTracking:
    turn: int = 0
```

- `QueryState`：持久化对话历史，跨 turn 累积
- `QueryTracking`：单次 query_loop 的追踪信息（turn 计数、未来可扩展 token_usage/latency 等）

### collect_tool_calls()

从流式事件列表中提取并组装完整的 ToolCall 列表：

```python
def collect_tool_calls(events: list[Event]) -> list[ToolCall]:
    # 监听 ToolCallStart → 创建 buffer
    # 监听 ToolCallDelta → 拼接 arguments_json_delta
    # 按 ToolCallStart 出现顺序返回完整 ToolCall
```

## ToolUseContext（context/tool_use.py）

Query loop 的"神经系统"——不存储业务数据（那是 state 的职责），而是提供工具列表、权限、中断信号、追踪信息等一切基础设施的访问入口。每次迭代通过展开 `{ ...toolUseContext, queryTracking, messages }` 来传递到下一轮。

通过 **Callable 注入**（而非直接依赖 ToolRegistry/StreamingToolExecutor），实现 query_engine 与 tools 层的完全解耦。

```python
class ToolUseContext:
    def __init__(
        self,
        *,
        get_schemas: Callable[[], list[dict[str, Any]]],
        execute: Callable[[list[ToolCall]], AsyncGenerator[ToolResultEvent, None]],
        check_permission: Callable[[str], bool] | None = None,
        is_interrupted: Callable[[], bool] | None = None,
        on_trace: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None: ...

    def get_tool_schemas(self) -> list[dict[str, Any]]: ...
    async def execute(self, tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]: ...
    def check_permission(self, tool_name: str) -> bool: ...
    @property
    def is_interrupted(self) -> bool: ...
    def trace(self, event: str, **kwargs: Any) -> None: ...
```

| 回调 | 默认值 | 用途 |
|------|--------|------|
| `get_schemas` | 无（必须提供） | 返回给 LLM 的工具 schema 列表 |
| `execute` | 无（必须提供） | 执行 tool_calls 并 yield ToolResultEvent |
| `check_permission` | 全部允许 | Plan/Build 模式下的工具权限控制 |
| `is_interrupted` | 永不中断 | 用户中断信号（Ctrl+C） |
| `on_trace` | 空操作 | 追踪事件（stream_start/stream_end） |

## QueryEngine（query_engine/engine.py）

```python
StreamFn = Callable[
    [list[Message], list[dict[str, Any]]],
    AsyncGenerator[Event, None],
]

PostTurnHook = Callable[[QueryState], Awaitable[None]]


class QueryEngine:
    def __init__(
        self,
        stream_fn: StreamFn,
        tool_use_ctx: ToolUseContext,
        completion_queue: asyncio.Queue[AgentCompletionEvent] | None = None,
        agent_event_queue: asyncio.Queue[Event] | None = None,
        post_turn_hook: PostTurnHook | None = None,
        model: str = "",
    ) -> None: ...
    
    async def submit_message(
        self, prompt: str, state: QueryState | None = None,
    ) -> AsyncGenerator[Event, None]:
        # 1. 若 state 为 None，创建新 QueryState
        # 2. 追加用户消息到 state.messages
        # 3. yield from _query_loop(state)

    async def _query_loop(self, state: QueryState) -> AsyncGenerator[Event, None]:
        # while True:
        #   中断检查
        #   排空 completion_queue / agent_event_queue
        #   自动压缩检查（should_auto_compact）
        #   调用 stream_fn 流式推理 → yield 事件
        #   被动压缩（ContextLengthExceeded 时）
        #   collect_tool_calls → 权限检查 → 执行工具
        #   追加 assistant + tool messages 到 state
        #   记录 TurnRecord → post_turn_hook
        # 退出后排空剩余队列事件
```

### 外部组装示例

```python
# 在应用入口（TUI ChatScreen / CLI chat / AgentManager）组装所有依赖

# 1. 组装 system prompt（由调用层负责，不在 QueryEngine 中）
from mini_cc.context.system_prompt import SystemPromptBuilder
prompt_builder = SystemPromptBuilder()
system_content = prompt_builder.build(env_info, mode="build")

# 2. 创建对话状态，system prompt 作为 messages[0]
state = QueryState(messages=[Message(role=Role.SYSTEM, content=system_content)])

# 3. 组装依赖
provider = OpenAIProvider(model="...", base_url="...", api_key="...")
registry = create_default_registry()
executor = StreamingToolExecutor(registry)

tool_use_ctx = ToolUseContext(
    get_schemas=registry.to_api_format,
    execute=executor.run,
    check_permission=lambda name: name in allowed_tools,
    is_interrupted=lambda: shutdown_event.is_set(),
)

engine = QueryEngine(stream_fn=provider.stream, tool_use_ctx=tool_use_ctx)

# 4. 斜杠命令由 UI 层处理，普通消息才进入 engine
async for event in engine.submit_message(user_input, state):
    handle(event)
```

## StreamingToolExecutor（tool_executor/executor.py）

解决的核心问题：如何在模型流式输出的同时，安全地执行工具。

```python
class StreamingToolExecutor:
    def __init__(self, tool_registry: ToolRegistry): ...

    async def run(self, tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
        # 将 tool_calls 分为并发安全组和不安全组
        # 安全工具用 asyncio.as_completed 并行执行
        # 不安全工具串行执行
        # yield 已完成的结果
```

并发安全规则：
- `file_read`、`glob`、`grep` → 并发安全（只读）
- `file_edit`、`file_write`、`bash` → 非并发安全（有副作用）
- 安全工具之间可并行，不安全工具必须串行
- 所有安全工具先执行完毕，再串行执行不安全工具

错误处理：
- 未注册的工具 → yield `ToolResultEvent(success=False, output="Unknown tool: ...")`
- 非法 JSON arguments → yield `ToolResultEvent(success=False, output="Invalid JSON arguments")`

## LLMProvider（providers/base.py）

```python
class LLMProvider(Protocol):
    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> AsyncGenerator[Event, None]: ...
```

### OpenAIProvider（providers/openai.py）

```python
class OpenAIProvider:
    def __init__(self, model: str, base_url: str, api_key: str): ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> AsyncGenerator[Event, None]:
        # 调用 openai SDK，检查 streaming chunk 字段判断类型，
        # 转换为统一的 Event 类型
```

OpenAI 兼容模式，通过自定义 `base_url` 支持 GLM、DeepSeek、Qwen 等。

配置通过 `.env` 文件读取：

```env
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
OPENAI_MODEL=glm-4-plus
```

## 终止条件

1. **正常完成** — 模型输出纯文本，没有调用任何工具
2. **权限拒绝** — 所有 tool_calls 被 check_permission 拒绝
3. **用户中断** — `is_interrupted` 返回 True（由外部注入 Ctrl+C 信号）
4. **API 错误** — 网络断开 / 限流 / Token 超额

## 模块文件结构

```
src/mini_cc/
├── context/
│   ├── __init__.py
│   ├── system_prompt.py        # SystemPromptBuilder（静态 prompt + 环境信息 + AGENTS.md + 记忆）
│   └── tool_use.py             # ToolUseContext（Callable 注入）
├── query_engine/
│   ├── __init__.py             # 导出 state 中的类型（不导入 engine，避免循环依赖）
│   ├── engine.py               # QueryEngine + submit_message + _query_loop
│   └── state.py                # Role, Message, Event, QueryState, QueryTracking, collect_tool_calls()
├── tool_executor/
│   ├── __init__.py
│   └── executor.py             # StreamingToolExecutor
├── providers/
│   ├── __init__.py
│   ├── base.py                 # LLMProvider Protocol
│   └── openai.py               # OpenAIProvider
├── compression/
│   └── compressor.py           # compress_messages, should_auto_compact, replace_with_summary
├── task/
│   └── models.py               # AgentCompletionEvent
└── tools/
    ├── base.py                 # BaseTool, ToolRegistry
    └── ...
```

### 依赖方向

```
providers/openai.py ──→ query_engine/state.py ←── context/tool_use.py
                                                           ↑
query_engine/engine.py ──→ context/tool_use.py             │
                  ├────→ query_engine/state.py             │
                  ├────→ compression/compressor.py         │
                  └────→ task/models.py                    │
                                                           │
tool_executor/executor.py ──→ query_engine/state.py ───────┤
                  └─────────→ tools/base.py                │
                                                           │
context/system_prompt.py ──→ query_engine/state.py ────────┘
```

无循环依赖。`context/tool_use.py` 只依赖 `query_engine/state.py`（纯类型模块），不依赖 `query_engine/engine.py`。`query_engine/engine.py` 通过 `compression/compressor.py` 实现上下文压缩，通过 `task/models.py` 的 `AgentCompletionEvent` 处理子 agent 完成通知。

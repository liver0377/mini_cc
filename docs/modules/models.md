# 数据模型 (models)

数据模型模块定义了系统各模块间共享的所有数据结构，包括消息、事件、查询状态、任务和智能体配置。

## 模块结构

```
models/
├── message.py     # 消息模型（Role、Message、ToolCall）
├── events.py      # 事件模型（Agent Loop 产生的所有事件类型）
├── query.py       # 查询状态模型（QueryState、跟踪信息）
├── task.py        # 任务模型（Task、TaskStatus）
└── agent.py       # 智能体模型（AgentConfig、AgentStatus、AgentBudget）
```

## 模型关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                         Message 层                               │
│                                                                 │
│  ┌──────────┐     ┌────────────┐     ┌───────────────────┐     │
│  │  Role    │     │ ToolCall   │     │  Message          │     │
│  │ (StrEnum)│     │ (BaseModel)│     │  (BaseModel)      │     │
│  │          │     │            │     │                   │     │
│  │ system   │     │ id         │     │ role: Role        │     │
│  │ user     │     │ name       │     │ content: str|None │     │
│  │ assistant│     │ arguments  │     │ tool_calls: list  │     │
│  │ tool     │     └────────────┘     │ source: MsgSource │     │
│  └──────────┘                        │ metadata: dict    │     │
│                                      └───────────────────┘     │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                         Event 层                                 │
│                                                                 │
│  Event = TextDelta | ToolCallStart | ToolCallDelta               │
│        | ToolCallEnd | ToolResultEvent                           │
│        | AgentStartEvent | AgentTextDeltaEvent                   │
│        | AgentToolCallEvent | AgentToolResultEvent               │
│        | AgentCompletionEvent                                    │
│        | CompactOccurred | ContextLengthExceededError            │
│                                                                 │
│  ┌──────────────────────────────────────────────┐               │
│  │ 所有 Event 均为 @dataclass(frozen=False)      │               │
│  └──────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       Query State 层                             │
│                                                                 │
│  ┌───────────────┐  ┌────────────────┐  ┌─────────────────┐    │
│  │ QueryState    │  │ ToolCallSummary│  │ QueryTracking   │    │
│  │ (BaseModel)   │  │                │  │                 │    │
│  │               │  │ name           │  │ turns: list     │    │
│  │ messages      │  │ duration_ms    │  │   [TurnRecord]  │    │
│  │ turn_count    │  │ success        │  │ total_tokens    │    │
│  └───────────────┘  └────────────────┘  │ total_duration  │    │
│                                         └─────────────────┘    │
│                                         ┌─────────────────┐    │
│                                         │ TurnRecord      │    │
│                                         │ role            │    │
│                                         │ tool_calls      │    │
│                                         │ duration_ms     │    │
│                                         │ token_count     │    │
│                                         └─────────────────┘    │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       Task 层                                    │
│                                                                 │
│  ┌──────────┐  ┌────────────┐  ┌──────────────────────────┐   │
│  │TaskType  │  │ TaskStatus │  │ Task (BaseModel)          │   │
│  │(StrEnum) │  │ (StrEnum)  │  │                          │   │
│  │          │  │            │  │ id                       │   │
│  │ edit     │  │ pending    │  │ type: TaskType           │   │
│  │ search   │  │ running    │  │ status: TaskStatus       │   │
│  │ test     │  │ completed  │  │ description              │   │
│  │ analyze  │  │ failed     │  │ dependencies: list[str]  │   │
│  │ ...      │  │ cancelled  │  │ result: str|None         │   │
│  └──────────┘  └────────────┘  │ error: str|None          │   │
│                                └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       Agent 层                                   │
│                                                                 │
│  ┌──────────────┐  ┌───────────────────────────────────────┐   │
│  │ AgentStatus  │  │ AgentConfig                           │   │
│  │ (StrEnum)    │  │                                       │   │
│  │              │  │ task: str        # 任务描述             │   │
│  │ pending      │  │ scope: list[str]  # 文件作用域         │   │
│  │ running      │  │ role: str        # 角色               │   │
│  │ completed    │  │ readonly: bool    # 是否只读           │   │
│  │ failed       │  └───────────────────────────────────────┘   │
│  │ cancelled    │                                                │
│  └──────────────┘                                               │
│                                                                 │
│  ┌──────────────┐  ┌───────────────────────────────────────┐   │
│  │ AgentId      │  │ AgentBudget                           │   │
│  │              │  │                                       │   │
│  │ 自动生成的   │  │ max_tokens: int   # token 上限         │   │
│  │ 唯一标识符   │  │ max_turns: int     # 最大轮次          │   │
│  └──────────────┘  └───────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## 模型类型选择原则

| 场景 | 类型 | 原因 |
|------|------|------|
| API 序列化 / 需要验证 | Pydantic `BaseModel` | `Message`, `ToolCall`, `QueryState`, `Task`, `AgentConfig` |
| 内部事件 / 不可变配置 | `@dataclass` | 所有 Event 类型、`Theme` |
| 枚举类型 | `StrEnum` | `Role`, `AgentStatus`, `TaskStatus`, `TaskType` |
| 接口定义 | `@runtime_checkable` Protocol | `LLMProvider` |

## 事件详细分类

### LLM 响应事件

| 事件 | 产生者 | 消费者 | 说明 |
|------|--------|--------|------|
| `TextDelta` | LLMProvider | REPL/TUI | 文本增量片段 |
| `ToolCallStart` | LLMProvider | REPL/TUI | 工具调用开始 |
| `ToolCallDelta` | LLMProvider | REPL/TUI | 工具参数增量 |
| `ToolCallEnd` | LLMProvider | REPL/TUI | 工具调用结束 |

### 工具执行事件

| 事件 | 产生者 | 消费者 | 说明 |
|------|--------|--------|------|
| `ToolResultEvent` | StreamingToolExecutor | REPL/TUI/QueryEngine | 工具执行结果 |

### 智能体事件

| 事件 | 产生者 | 消费者 | 说明 |
|------|--------|--------|------|
| `AgentStartEvent` | SubAgent | EventBus → Coordinator | 智能体启动 |
| `AgentTextDeltaEvent` | SubAgent | EventBus → Coordinator | 智能体文本增量 |
| `AgentToolCallEvent` | SubAgent | EventBus → Coordinator | 智能体工具调用 |
| `AgentToolResultEvent` | SubAgent | EventBus → Coordinator | 智能体工具结果 |
| `AgentCompletionEvent` | SubAgent | EventBus → Coordinator | 智能体完成 |

### 系统事件

| 事件 | 产生者 | 消费者 | 说明 |
|------|--------|--------|------|
| `CompactOccurred` | CompactionController | REPL/TUI | 上下文压缩发生 |
| `ContextLengthExceededError` | LLMProvider | QueryEngine | 上下文超限 |

## 辅助函数

`collect_tool_calls()` — 事件收集器

从事件流中收集所有完整的工具调用，将分散的 `ToolCallStart` + `ToolCallDelta` + `ToolCallEnd` 组装为完整的 `ToolCall` 对象列表。

```
事件流:
  ToolCallStart(id="1", name="bash")
  ToolCallDelta(id="1", arguments='{"comma')
  ToolCallDelta(id="1", arguments='nd":"ls"}')
  ToolCallEnd(id="1")
  ToolCallStart(id="2", name="grep")
  ...

collect_tool_calls() 输出:
  [
    ToolCall(id="1", name="bash", arguments='{"command":"ls"}'),
    ToolCall(id="2", name="grep", arguments=...),
  ]
```

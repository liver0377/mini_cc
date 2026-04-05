# Query Engine 设计

## 是什么

Query Engine 是 agent 循环的**纯编排器**。它接收已组装好的对话状态，驱动 LLM 流式推理 → 工具执行 → 结果反馈的多轮循环，并将事件实时推送给 UI 层。

**它不负责**：system prompt 拼装、斜杠命令解析、LLM 协议细节、工具实现。

## 职责边界

| 职责 | 归属 | 说明 |
|------|------|------|
| System prompt 拼装 | context/system_prompt.py | 由调用层注入为首条 system 消息 |
| 斜杠命令解析 | TUI ChatScreen / CLI | /help、/mode、/compact 等 |
| LLM 流式通信 | providers/ | 通过 StreamFn 注入 |
| 工具执行 | tool_executor/ | 通过 ToolUseContext 注入 |
| **Agent 循环驱动** | **query_engine/** | **本模块** |

## 整体流程

```
用户输入（斜杠命令已由 UI 层过滤）
       │
       ▼
┌─ 调用层 ────────────────────────────────────────────┐
│  SystemPromptBuilder.build() → 系统 prompt            │
│  QueryState 初始化（首条 system 消息）                  │
│  engine.submit_message(prompt, state)                 │
└──────────────────────────────────────────────────────┘
       │
       ▼  AsyncGenerator[Event] 逐事件流式返回
┌─ QueryEngine._query_loop() ─────────────────────────┐
│                                                       │
│  while True:                                          │
│                                                       │
│    ① 中断检查                                         │
│       is_interrupted? → break                         │
│                                                       │
│    ② 排空后台事件                                      │
│       排空 completion_queue / agent_event_queue       │
│       → yield Agent*Event 给 UI                       │
│                                                       │
│    ③ 主动压缩                                         │
│       token 数超阈值? → 压缩 → yield CompactOccurred  │
│                                                       │
│    ④ 流式调用 LLM                                     │
│       stream_fn(messages, schemas)                    │
│       → yield TextDelta / ToolCall* 实时推送          │
│       → 若超长: 被动压缩 → continue 回到①              │
│                                                       │
│    ⑤ 工具调用                                         │
│       收集并拼装 ToolCall                              │
│       无工具调用 → 检查后台 Agent                       │
│         ├── 有运行中 Agent → 等待完成 → 汇总 → 继续循环│
│         └── 无运行中 Agent → break 正常结束             │
│       有工具调用 → 权限过滤 → 执行 → yield 结果         │
│                                                       │
│    ⑥ 状态更新                                         │
│       追加 assistant + tool messages 到 state          │
│       记录 TurnRecord（耗时 / 工具摘要）               │
│       await post_turn_hook（记忆提取）                 │
│                                                       │
│    loop back to ①                                     │
│                                                       │
│  退出后: 排空剩余 completion / agent 事件              │
└───────────────────────────────────────────────────────┘
```

## 事件类型

Query Engine 产出 11 种事件，通过 AsyncGenerator 实时推送给 UI。

### LLM 流式事件（由 Provider 产生）

| 事件 | 含义 |
|------|------|
| TextDelta | LLM 输出的文本片段 |
| ToolCallStart | 工具调用开始（携带 ID 和工具名） |
| ToolCallDelta | 工具参数的增量 JSON 片段 |
| ToolCallEnd | 工具参数传输完毕 |

### 工具执行事件（由 ToolUseContext 产生）

| 事件 | 含义 |
|------|------|
| ToolResultEvent | 工具执行结果（成功/失败 + 输出） |

### 上下文管理事件（由 Engine 产生）

| 事件 | 含义 |
|------|------|
| CompactOccurred | 上下文压缩已发生（reason: auto / reactive） |

### 子 Agent 事件（从后台队列排空）

| 事件 | 含义 |
|------|------|
| AgentStartEvent | 子 Agent 启动 |
| AgentTextDeltaEvent | 子 Agent 文本输出片段 |
| AgentToolCallEvent | 子 Agent 调用了工具 |
| AgentToolResultEvent | 子 Agent 工具执行结果 |
| AgentCompletionEvent | 子 Agent 完成（从 completion_queue 排空） |

## 注入的依赖

Query Engine 通过构造函数接收所有外部能力，自身不依赖任何具体实现。

### StreamFn

| 属性 | 说明 |
|------|------|
| 签名 | 接收 messages 和 schemas，返回 AsyncGenerator[Event] |
| 实际注入 | OpenAIProvider.stream |
| 用途 | 流式调用 LLM，产出文本/工具调用事件 |
| 其他消费方 | compression（摘要）、memory（记忆提取） |

### ToolUseContext

"神经中枢"——通过 Callable 注入提供工具相关的所有基础设施：

| 回调 | 默认值 | 用途 |
|------|--------|------|
| get_schemas | 必须提供 | 返回给 LLM 的工具 schema 列表 |
| execute | 必须提供 | 执行 tool_calls 并 yield 结果 |
| check_permission | 全部允许 | Plan/Build 模式下的工具权限控制 |
| is_interrupted | 永不中断 | 用户中断信号 |
| trace | 空操作 | 调试追踪（stream_start / stream_end） |

### 两个异步队列

| 队列 | 元素类型 | 来源 | 用途 |
|------|---------|------|------|
| completion_queue | AgentCompletionEvent | 后台 readonly Agent 完成时 | 通知 UI 子 Agent 已完成，展示最终结果 |
| agent_event_queue | Event | 后台 readonly Agent 运行中 | 实时推送子 Agent 的文本输出、工具调用等 |

两个队列的排空时机：
- **循环内**：每轮开始时、工具执行间隙 → 保证实时性
- **循环退出后**：兜底排空 → 防止后台 Agent 事件丢失

### post_turn_hook

| 属性 | 说明 |
|------|------|
| 签名 | async (QueryState) → None |
| 当前用途 | MemoryExtractor：每轮结束后检查是否需要提取跨会话记忆 |
| 触发条件 | 积累 ≥ 4 条新消息后，后台异步执行，不阻塞主循环 |
| 设计意图 | 可扩展钩子——未来可接入计费、审计、会话保存等 |

## 上下文压缩策略

Query Engine 实现了两级压缩：

| 类型 | 时机 | 触发条件 | 可恢复 |
|------|------|---------|--------|
| **主动压缩** | 调用 LLM **之前** | should_auto_compact 估算接近上限 | 预防性，基于估算 |
| **被动压缩** | 调用 LLM **失败后** | API 返回 ContextLengthExceeded | 补救性，最多重试一次 |

被动压缩用 has_attempted_reactive 标志确保最多补救一次，避免无限循环。

## 后台 Agent 处理

当 LLM 完成一轮输出但没有工具调用时，QueryEngine 不会立即退出循环：

1. 检查是否有后台只读 Agent 仍在运行
2. 如果有，阻塞等待所有后台 Agent 完成
3. 收集所有完成结果，构建汇总消息
4. 将汇总作为新的用户消息追加到 state，继续循环
5. LLM 在下一轮中基于汇总结果生成最终回复

这确保了异步子 Agent 的结果总能被主 Agent 消化和整合。

## 终止条件

| 条件 | 说明 |
|------|------|
| 正常完成 | LLM 输出纯文本，无工具调用，且无运行中的后台 Agent → break |
| 权限拒绝 | 所有 tool_calls 被 check_permission 拒绝 → break |
| 用户中断 | is_interrupted 返回 True → break |
| API 错误 | 被动压缩后仍超长 → raise，由上层处理 |

## 模块结构

```
query_engine/
├── __init__.py     导出 models 中的类型（不导入 engine，避免循环依赖）
└── engine.py       QueryEngine + submit_message + _query_loop

models/             共享数据模型
├── message.py      Role, ToolCall, Message
├── events.py       事件 dataclass + Event union + collect_tool_calls()
├── query.py        QueryState, QueryTracking, TurnRecord
├── task.py         Task, TaskStatus, TaskType
└── agent.py        AgentConfig, AgentStatus, AgentId
```

### 依赖方向

- providers/ → models/
- context/tool_use.py → models/
- engine.py → context/tool_use.py、models/、compression/、task/
- models/ 是纯类型模块包，无副作用，所有模块都依赖它
- engine.py 不依赖任何具体 Provider 或 Tool 实现

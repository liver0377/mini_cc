# 运行时核心 (runtime)

运行时核心是系统的中枢，负责 Agent Loop 驱动、工具执行、子智能体协调和上下文压缩。它将 LLM 提供者、工具系统、上下文组装等底层模块整合为完整的执行流水线。

## 模块结构

```
runtime/
├── facade.py                   # 运行时门面（高层 API）
├── query/                      # Agent Loop 引擎
│   ├── engine.py               #   查询引擎主循环
│   ├── compaction.py           #   上下文压缩控制器
│   └── agent_coordinator.py    #   智能体完成协调器
├── execution/                  # 工具执行引擎
│   ├── executor.py             #   流式工具执行器
│   ├── policy.py               #   执行策略
│   └── factories.py            #   工厂函数
└── agents/                     # 多智能体管理（详见 agents.md）
```

## 架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                       RuntimeFacade                              │
│  高层 API：submit_message / run_agent / list_agents / cancel...  │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                       EngineContext                               │
│  中央上下文持有者：QueryEngine + PromptBuilder + AgentManager     │
│  + contextvars（run_id, mode, budget, interrupt）                │
└──────────┬──────────────────────┬───────────────────┬───────────┘
           │                      │                   │
           ▼                      ▼                   ▼
┌──────────────────┐  ┌──────────────────────┐  ┌────────────────┐
│   QueryEngine    │  │  StreamingTool       │  │  AgentManager  │
│                  │  │   Executor           │  │  (子智能体)     │
│  Agent Loop:     │  │                      │  │                │
│  1. 组装消息      │  │  · 安全工具并发执行    │  │  · 创建智能体   │
│  2. 流式推理      │  │  · 非安全工具串行执行  │  │  · 工作区隔离   │
│  3. 收集工具调用  │  │  · 超时控制           │  │  · 快照回滚     │
│  4. 执行工具      │  │                      │  │  · 完成协调     │
│  5. 压缩检查      │  └──────────────────────┘  └────────────────┘
│  6. 循环         │
│                  │  ┌──────────────────────┐
│  ┌────────────┐  │  │  ExecutionPolicy     │
│  │ Compaction │  │  │  · 只读强制          │
│  │ Controller │  │  │  · 工具白名单        │
│  └────────────┘  │  │  · 作用域路径验证     │
└──────────────────┘  │  · Bash 限制         │
                      └──────────────────────┘
```

## RuntimeFacade

运行时门面，对上层（CLI/TUI/Harness）提供统一的高层 API。

| 方法 | 说明 |
|------|------|
| `submit_message()` | 提交用户消息，返回事件异步生成器 |
| `run_agent()` | 同步运行一个子智能体（前台） |
| `start_background_agent()` | 启动后台子智能体（异步） |
| `list_agents()` | 列出所有活跃智能体 |
| `cancel_agents()` | 取消指定智能体 |
| `compact_state()` | 触发上下文压缩 |
| `new_query_state()` | 创建新的查询状态 |

## EngineContext

引擎上下文，是整个运行时的中央协调器：

```
EngineContext
├── 持有引用
│   ├── query_engine        # QueryEngine 实例
│   ├── prompt_builder      # SystemPromptBuilder 实例
│   ├── agent_manager       # AgentManager 实例
│   ├── lifecycle_bus       # AgentEventBus 实例
│   ├── memory_extractor    # MemoryExtractor 实例
│   └── compaction_ctrl     # CompactionController 实例
│
├── contextvars（协程安全）
│   ├── run_id              # 当前运行 ID
│   ├── mode                # 运行模式
│   ├── budget              # 预算控制
│   └── interrupt           # 中断标志
│
└── 核心方法
    ├── submit_message()    # 提交消息
    ├── new_query_state()   # 创建查询状态
    ├── execution_scope()   # 执行作用域上下文管理
    └── compact_state()     # 压缩上下文
```

## QueryEngine — Agent Loop 核心

QueryEngine 是整个系统的心脏，实现了 Agent Loop 模式：

```
                    ┌─────────────────┐
                    │   用户消息输入    │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  组装消息列表     │◄──────────────────┐
                    │  system + history│                   │
                    └────────┬────────┘                   │
                             │                            │
                    ┌────────▼────────┐                   │
                    │  LLM 流式推理    │                   │
                    └────────┬────────┘                   │
                             │                            │
                    ┌────────▼────────┐                   │
                    │  解析事件流       │                   │
                    │  TextDelta       │                   │
                    │  ToolCall*       │                   │
                    └────────┬────────┘                   │
                             │                            │
                    ┌────────▼────────┐                   │
                    │  yield 事件给上层 │                   │
                    └────────┬────────┘                   │
                             │                            │
                    ┌────────▼────────┐                   │
                    │  存在工具调用？   │                   │
                    └──┬──────────┬───┘                   │
                      是          否                       │
                       │          │                       │
              ┌────────▼───┐      │                       │
              │ 执行工具    │      │                       │
              │ 追加结果    │      │                       │
              └────────┬───┘      │                       │
                       │          │                       │
              ┌────────▼──────────▼──┐                    │
              │  检查压缩需求         │                    │
              │  CompactionController│────────────────────┘
              │  (若压缩则替换历史)   │    (循环回消息组装)
              └─────────────────────┘
```

**关键设计要点：**

- 每轮循环（turn）包含一次 LLM 调用 + 零或多次工具调用
- 工具调用结果追加到消息列表后，自动进入下一轮
- 循环终止条件：LLM 不再产生工具调用
- 每轮结束后检查压缩需求

## CompactionController — 上下文压缩

控制上下文窗口的使用，防止超出 LLM 限制：

```
CompactionController
├── should_compact_fn()    # 判断是否需要压缩
│   ├── 基于 token 估算阈值
│   └── 基于消息数量阈值
│
├── compact_fn()           # 执行压缩
│   ├── 保留最近 N 条消息
│   ├── 历史消息发送给 LLM 生成摘要
│   └── 返回新消息列表
│
└── replace_summary_fn()   # 用摘要替换历史
```

**两种压缩触发模式：**

| 模式 | 触发条件 | 行为 |
|------|----------|------|
| 自动压缩 | token 估算超过阈值 | 主动压缩历史消息 |
| 被动压缩 | 收到 ContextLengthExceeded 错误 | 强制压缩后重试 |

## StreamingToolExecutor — 工具执行器

```
StreamingToolExecutor
│
├── 输入：ToolCall 列表
│
├── 分类
│   ├── 安全工具（file_read, glob, grep, scan_dir）
│   │   └── asyncio.gather() 并发执行
│   │
│   └── 非安全工具（bash, file_write, file_edit, agent_tool...）
│       └── 串行逐一执行
│
├── 执行流程
│   ├── yield ToolResultEvent（每个工具结果）
│   └── 超时控制（可配置）
│
└── 错误处理
    ├── 工具错误 → ToolResult(success=False, error="...")
    └── 不抛异常，所有错误以结果形式返回
```

## ExecutionPolicy — 执行策略

控制工具执行的约束条件：

| 策略 | 说明 |
|------|------|
| 只读强制 | 禁止执行任何写入类工具 |
| 工具白名单 | 限制可用的工具集合 |
| 作用域路径验证 | 工具只能操作指定目录内的文件 |
| Bash 限制 | 限制可执行的 Bash 命令范围 |

## 工厂函数 (factories.py)

提供组件的创建和组装逻辑：

| 工厂 | 说明 |
|------|------|
| `ProviderFactory` | 根据 API 密钥和配置创建 LLM 提供者 |
| `ToolingFactory` | 创建工具注册表和工具实例 |
| `_EngineConfig` | 引擎配置数据类 |
| `load_dotenv()` | 加载环境变量配置 |

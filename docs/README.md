# Mini Claude Code 项目文档

> Mini Claude Code（`mini_cc`）是一个轻量级多智能体协作编程助手 CLI，采用纯 Python 构建，通过异步 Agent Loop 流式处理 LLM 响应，执行工具调用，并经由 Textual TUI 或 prompt_toolkit REPL 渲染结果。

## 文档导航

### 架构设计

| 文档 | 说明 |
|------|------|
| [架构总览](architecture/overview.md) | 系统整体架构、分层设计、模块依赖关系图 |
| [数据流与事件系统](architecture/data-flow.md) | 事件驱动架构、Agent Loop 数据流、事件状态机 |

### 模块设计

| 文档 | 说明 |
|------|------|
| [应用层 (app)](modules/app.md) | CLI 入口、REPL 交互循环、Textual TUI 界面 |
| [运行时核心 (runtime)](modules/runtime.md) | QueryEngine Agent Loop、工具执行器、运行时门面 |
| [多智能体系统 (agents)](modules/agents.md) | Agent 管理器、子智能体调度、生命周期总线、快照回滚 |
| [上下文组装 (context)](modules/context.md) | 系统提示词构建、工具使用上下文、引擎上下文 |
| [LLM 提供者 (providers)](modules/providers.md) | 提供者协议、OpenAI 流式实现 |
| [数据模型 (models)](modules/models.md) | 消息、事件、查询状态、任务、智能体模型 |
| [工具系统 (tools)](modules/tools.md) | 工具注册表、执行策略、各工具实现 |
| [任务队列 (task)](modules/task.md) | 持久化任务服务、状态机、依赖追踪 |
| [自主运行线束 (harness)](modules/harness.md) | Run Harness 自动化执行框架、调度、裁判、监督循环 |
| [横切特性 (features)](modules/features.md) | 长期记忆、上下文压缩 |

## 项目源码结构

```
src/mini_cc/
├── __init__.py              # 包版本
├── __main__.py              # 入口点
├── app/                     # 应用层：CLI、REPL、TUI
├── context/                 # 系统提示词组装 + 工具使用上下文
├── features/                # 横切特性：记忆、压缩
├── harness/                 # 自主运行线束
├── models/                  # 共享数据模型
├── providers/               # LLM 提供者协议与实现
├── runtime/                 # 运行时核心
│   ├── agents/              #   多智能体管理
│   ├── execution/           #   工具执行引擎
│   └── query/               #   Agent Loop 引擎
├── task/                    # 任务队列服务
└── tools/                   # 工具实现
```

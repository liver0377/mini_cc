# Mini Claude Code 设计文档

本目录包含 Mini Claude Code 各子系统的设计文档，所有文档使用中文编写。

## 文档索引

| 模块 | 文档 | 说明 |
|------|------|------|
| **Agent Loop** | [agent-loop/README.md](./agent-loop/README.md) | 核心交互循环与流式事件机制 |
| | [agent-loop/query-engine.md](./agent-loop/query-engine.md) | QueryEngine 编排器、状态模型、事件类型、依赖注入 |
| **Multi-Agent** | [multi-agent/README.md](./multi-agent/README.md) | 主从多 Agent 架构总览（写 Agent / 只读 Agent / Fork） |
| | [multi-agent/agent.md](./multi-agent/agent.md) | Agent 抽象（AgentId / AgentConfig / SubAgent）、生命周期、AgentTool、AgentManager |
| | [multi-agent/infrastructure.md](./multi-agent/infrastructure.md) | 隔离策略（worktree + 快照）、消息同步、结果反馈循环、output 持久化 |
| | [multi-agent/task.md](./multi-agent/task.md) | 统一 Task 系统（类型、依赖、生命周期、TaskService） |
| **Context** | [context/README.md](./context/README.md) | 系统提示词组装（静态模板 + 动态环境信息 + AGENTS.md） |
| **Tools** | [tools/README.md](./tools/README.md) | 工具设计（文件 / Bash / 搜索）与工具注册表 |
| **Memory** | [memory/design.md](./memory/design.md) | 中期记忆系统（跨会话持久化、四类分类、自动提取） |
| **Compression** | [compression/design.md](./compression/design.md) | 上下文压缩（自动压缩 / 反应式压缩 / 手动 `/compact`、tiktoken 计数） |
| **Security** | [security/README.md](./security/README.md) | 安全设计（Sandbox 限制、Plan/Build 模式） |

## 架构概览

```
用户输入
    │
    ▼
┌─ QueryEngine ──────────────────────────────────────┐
│  submit_message() → AsyncGenerator[Event]           │
│    ├── 组装 system prompt（Context 模块）            │
│    ├── 流式调用 LLM（Provider 模块）                 │
│    ├── 事件状态机（TextDelta / ToolCall* / Result）  │
│    ├── 调度工具执行（ToolExecutor + Tools）           │
│    └── 思考-行动循环直到终止                          │
└─────────────────────────────────────────────────────┘
    │                              │
    │ 创建子 Agent                 │ 异步执行
    ▼                              ▼
┌─ Multi-Agent ──────┐    ┌─ Task 系统 ─────┐
│ 写 Agent（同步）    │    │ 依赖追踪         │
│ 只读 Agent（异步）  │    │ 并发控制（flock） │
│ Fork Agent         │    │ 生命周期管理      │
└────────────────────┘    └──────────────────┘
```

## 系统提示词构成

```
1. 静态 prompt（intro.md, rules.md, caution.md, tool_guide.md）
2. 环境信息（<env> ... </env>）
3. AGENTS.md（用户手动维护的项目指令）
4. session-memory.md 摘要（上下文压缩，会话级）
5. MEMORY.md 索引（中期记忆，跨会话级）
```

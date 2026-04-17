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
| **Context** | [context/README.md](./context/README.md) | 系统提示词组装（静态模板 + 动态环境信息 + AGENTS.md + 记忆索引） |
| **Tools** | [tools/README.md](./tools/README.md) | 工具设计（文件 / Bash / 搜索 / Agent）、并发执行模型、工具注册表 |
| **TUI** | [tui/README.md](./tui/README.md) | Textual TUI 架构（屏幕、组件、斜杠命令、补全、Agent 管理面板） |
| **Memory** | [memory/design.md](./memory/design.md) | 中期记忆系统（跨会话持久化、四类分类、自动提取） |
| **Compression** | [compression/design.md](./compression/design.md) | 上下文压缩（自动压缩 / 反应式压缩 / 手动 `/compact`、tiktoken 计数） |
| **Harness** | [harness/design.md](./harness/design.md) | 长时运行 Harness（Run 生命周期、Supervisor、Step、Checkpoint、Policy） |
| | [harness/orchestrator-refactor.md](./harness/orchestrator-refactor.md) | Harness 多 Agent 编排重构方案（Orchestrator / Dispatcher / WorkItem / 失败分型 / 落地清单） |
| | [harness/task-specific-audit.md](./harness/task-specific-audit.md) | 任务专项审计（profile、专项 artifact、专项 judge、任务完成度文档化） |
| **Iteration** | [iteration/design.md](./iteration/design.md) | 迭代优化系统（每轮复盘、评分、约束注入、下一轮改进） |
| | [iteration/runtime.md](./iteration/runtime.md) | 运行期迭代记录（journal、snapshot、review 持久化格式与自动调试行为） |
| **Security** | [security/README.md](./security/README.md) | 安全设计（Sandbox 限制、Plan/Build 模式） |

## 架构概览

```
用户输入 ──→ REPL / TUI ──→ QueryEngine
                                  │
                   ┌──────────────┼──────────────┐
                   │              │              │
                   ▼              ▼              ▼
             Context 模块    Provider 模块   ToolExecutor
             (提示词组装)    (LLM 流式调用)   (工具执行调度)
                                                  │
                                          ┌───────┴───────┐
                                          │               │
                                          ▼               ▼
                                    安全工具(并发)    危险工具(串行)
                                    file_read         file_edit
                                    glob              file_write
                                    grep              bash
                                                      agent
                                                          │
                                                  ┌───────┴───────┐
                                                  │               │
                                                  ▼               ▼
                                            写 Agent          只读 Agent
                                            (同步阻塞)        (异步后台)
                                            直写主工作区       worktree 隔离
                                            快照备份           结果队列通知

         ┌────────────────────────────────────────────────────────────┐
         │                     横切关注点                               │
         │  Compression (上下文压缩)  │  Memory (跨会话记忆)            │
         │  Task (统一任务追踪)       │  Security (Plan/Build 模式)     │
         └────────────────────────────────────────────────────────────┘
```

## 系统提示词构成

```
1. 静态 prompt（intro.md, rules.md, caution.md, tool_guide.md）
2. 环境信息（<env> 工作目录、git 状态、平台、模型 </env>）
3. AGENTS.md（用户手动维护的项目指令）
4. MEMORY.md 索引（中期记忆，跨会话级，≤ 200 行）
```

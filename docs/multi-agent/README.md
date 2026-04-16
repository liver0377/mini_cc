# Multi-Agent 架构设计

## 概述

系统采用**主-从（Master-Worker）** 多 Agent 架构。主 Agent 是用户对话的唯一入口，拥有独立的 QueryEngine 和 QueryState。子 Agent 由主 Agent 通过 AgentTool 创建，各自拥有独立的 QueryEngine 和 QueryState，但共享主 Agent 的 LLM Provider（相同 API Key 和模型）。

子 Agent 的工具列表**不包含 AgentTool**，因此无法递归创建更深层的子 Agent。

## 子 Agent 类型

系统将子 Agent 分为两类，通过 readonly 参数区分：

| 类型 | 用途 | 工具集 | 执行方式 | 文件隔离 | 回滚机制 |
|------|------|--------|----------|----------|----------|
| **写 Agent** (readonly=false) | 修改代码、修复 bug、重构 | 全量（file_edit, file_write, bash, file_read, glob, grep, scan_dir, plan_agents） | 同步阻塞 | 无，直写主工作区 | SnapshotService 文件快照 |
| **只读 Agent** (readonly=true) | 探索代码库、分析架构、搜索代码 | 只读（file_read, glob, grep, bash, scan_dir, plan_agents） | 异步后台 | 无，共享主工作区（仅只读工具） | 无（不修改文件） |

此外，写 Agent 支持 **Fork 模式**（fork=true）——深拷贝父 Agent 的完整对话上下文作为初始状态，继承父 Agent 的对话历史继续工作。Fork 模式仅对写 Agent 有意义，因为只读 Agent 不修改文件，无需继承上下文。

### 并发冲突防护

系统采用四层防护：

| 层级 | 机制 | 说明 |
|------|------|------|
| Scope 隔离 | `_assert_write_scope_available()` | 新 write Agent 的 scope 与活跃 write Agent 无路径前缀重叠 |
| 读写工具分离 | `create_readonly_registry()` | Readonly Agent 无 file_edit / file_write |
| 文件快照 | `SnapshotService` | Write Agent 修改前自动备份，可 `restore_all()` 回滚 |
| 工具串行 | `StreamingToolExecutor` | unsafe 工具（file_edit, file_write, bash）串行执行 |

### 已知局限

- **无文件系统级隔离**：所有 Agent 共享同一工作目录。Readonly Agent 可能看到 Write Agent 正在修改的中间状态。
- **Staleness 检测是事后的**：版本戳（version stamp）在创建和完成时各取一次，只在完成事件中标记 `is_stale`，不自动重试。
- **Readonly Agent 之间无一致性保证**：多个并行 Readonly Agent 如果在 Write Agent 活跃期间运行，可能对同一文件看到不同版本。

## 整体架构

```
                         ┌──────────────────────┐
                         │       主 Agent        │
                         │  QueryEngine + State  │
                         └──────┬───────────────┘
                                │
                          AgentTool 调用
                                │
                ┌───────────────┴───────────────┐
                ▼                               ▼
        ┌──────────────┐                ┌──────────────┐
        │  写子 Agent   │                │ 只读子 Agent  │
        │  全量工具      │                │ 只读工具      │
        │  直写主工作区  │                │ 共享主工作区  │
        │  快照备份      │                │ 版本戳校验    │
        │  同步阻塞      │                │ 异步后台      │
        └──────┬───────┘                └──────┬───────┘
               │                               │
               │ 同步返回结果              异步通知
               │                         Queue + 结果反馈
               ▼                               ▼
        主 Agent 收到结果              主 Agent 收到通知
        （阻塞等待）                （轮询后二次提交 LLM 汇总）
```

## 核心概念

| 概念 | 说明 |
|------|------|
| **AgentId** | 8 位十六进制短随机字符串，全局唯一标识一个 Agent |
| **AgentConfig** | Agent 的不可变配置（ID、工作目录、是否 fork、是否 readonly、超时阈值） |
| **SubAgent** | 子 Agent 运行时对象，持有独立的 QueryEngine 和 QueryState |
| **AgentTool** | LLM 创建子 Agent 的唯一工具入口，支持 readonly 和 fork 参数 |
| **AgentManager** | 管理所有活跃子 Agent 的生命周期（创建、注册、清理） |
| **SnapshotService** | 写 Agent 的文件级快照备份服务，支持回滚（不操作 git） |
| **TaskService** | 统一的异步任务追踪系统，管理 local_agent 和 local_bash 两种任务 |

## 设计决策

### 为什么不做并行写

业界共识是写操作串行、读操作并行。多 Agent 并行修改同一文件时，无论用 diff、patch 还是 cherry-pick，都无法保证语义正确。Codex CLI、Claude Code、Devin、Aider 均不并行写。

### 为什么写 Agent 直写主工作区

直接写入主工作区，变更立即可见，无需合并步骤。这与 Codex CLI、Claude Code 的策略一致。省去了 worktree 创建、diff 收集、patch 应用的完整链路。

### 为什么不用 git worktree 做隔离

1. 只读 Agent 不修改文件，用 worktree 的主要收益（防止写冲突）不适用
2. Worktree 的创建和销毁有延迟，与异步 Agent 的快速启停节奏不匹配
3. 硬链接在部分文件系统（如 WSL 挂载的 Windows 盘）上有兼容问题
4. 当前方案（读写工具分离 + scope 检查 + 版本戳）已能覆盖主要冲突场景

未来如果需要更强的隔离（如支持并行写），可以在 V2 阶段引入 per-agent 临时目录 + diff apply 机制。

## 文档索引

| 文档 | 内容 |
|------|------|
| [agent.md](./agent.md) | Agent 抽象（AgentId / AgentConfig / SubAgent）、生命周期、AgentTool、AgentManager |
| [infrastructure.md](./infrastructure.md) | 隔离策略（worktree + 快照）、消息同步、结果反馈循环、output 持久化 |
| [task.md](./task.md) | 统一 Task 系统（类型、依赖、生命周期、TaskService） |

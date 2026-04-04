# Multi-Agent 架构设计

## 概述

系统采用 **主-从（Master-Worker）** 多 Agent 架构。主 Agent 是用户对话的唯一入口，拥有独立的 QueryEngine 和 QueryState。子 Agent 由主 Agent 通过 AgentTool 创建，各自拥有独立的 QueryEngine 和 QueryState，但共享主 Agent 的 LLM Provider（相同 API Key 和模型）。

子 Agent 的工具列表**不包含 AgentTool**，因此无法递归创建更深层的子 Agent。

## 子 Agent 类型

系统将子 Agent 分为两类，通过 `readonly` 参数区分：

| 类型 | 用途 | 工具集 | 执行方式 | 文件隔离 | 回滚机制 |
|------|------|--------|----------|----------|----------|
| **写 Agent** (readonly=false) | 修改代码、修复 bug、重构 | 全量（file_edit, file_write, bash, file_read, glob, grep） | 同步阻塞 | 无，直写主工作区 | SnapshotService 文件快照 |
| **只读 Agent** (readonly=true) | 探索代码库、分析架构、搜索代码 | 只读（file_read, glob, grep, bash） | 异步后台 | git worktree | 无（不修改文件） |

此外，写 Agent 支持 **Fork 模式**（fork=true）——深拷贝父 Agent 的完整对话上下文作为初始状态，继承父 Agent 的对话历史继续工作。Fork 模式仅对写 Agent 有意义，因为只读 Agent 不修改文件，无需继承上下文。

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
        │  直写主工作区  │                │ worktree 隔离 │
        │  快照备份      │                │ 异步后台      │
        │  同步阻塞      │                │               │
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

### 为什么不用 git commit 做回滚

1. 不污染用户 git 历史——`git log` 不出现 agent commit，`git status` 只显示文件变更
2. 避免 `git add -A` "偷走"用户未提交的变更
3. 用户自主决定何时 commit，agent 不越权

系统选择**文件系统快照备份**——通过 SnapshotService 在工具执行前备份原始文件，回滚时从备份恢复，完全在应用层完成。

### 为什么只读 Agent 仍用 worktree

1. 多个只读 Agent 可并行运行，各自在独立 worktree 中互不干扰
2. 只读 Agent 的 bash 命令可能产生副作用（如生成 `__pycache__`），worktree 隔离避免污染主工作区
3. `git worktree add` 是硬链接，创建和销毁开销极小

## 文档索引

| 文档 | 内容 |
|------|------|
| [agent.md](./agent.md) | Agent 抽象（AgentId / AgentConfig / SubAgent）、生命周期、AgentTool、AgentManager |
| [infrastructure.md](./infrastructure.md) | 隔离策略（worktree + 快照）、消息同步、结果反馈循环、output 持久化 |
| [task.md](./task.md) | 统一 Task 系统（类型、依赖、生命周期、TaskService） |

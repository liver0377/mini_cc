# Multi-Agent 架构设计

## 概述

整个项目采用 **主-从（Master-Worker）** 多 Agent 架构。主 Agent 负责子 Agent 的调度，子 Agent 通过 `AgentTool` 创建——这是唯一的子 Agent 创建方式。

主 Agent 即现有的 `QueryEngine` 实例。子 Agent 拥有独立的 `QueryEngine` + `QueryState`，但共享主 Agent 的 LLM Provider（相同 api key / model）。

## 子 Agent 类型

| 类型 | 上下文 | 执行模式 | 超时行为 | Worktree |
|------|--------|----------|----------|----------|
| **同步 Agent** | 独立 | 前台阻塞主 Agent | 120s 后自动转异步 | 是 |
| **异步 Agent** | 独立 | 后台运行，通过消息队列通知主 Agent | 无超时限制 | 是 |
| **Forked Agent** | 继承父 Agent 完整对话上下文 | 前台/后台均可 | 同同步 Agent | 是 |

## 整体架构

```
                        ┌──────────────────────┐
                        │       主 Agent        │
                        │  QueryEngine + State  │
                        └──────┬───────────────┘
                               │
                         AgentTool 调用
                               │
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
         ┌──────────┐  ┌──────────┐  ┌──────────────┐
         │ 同步子Agent│  │ 异步子Agent│  │  Forked Agent│
         │ 独立 ctx  │  │ 独立 ctx  │  │  共享父 ctx  │
         │ worktree  │  │ worktree  │  │  worktree    │
         └─────┬────┘  └─────┬────┘  └──────┬───────┘
               │             │               │
               │    asyncio.Queue            │
               │    + .output 持久化         │
               ▼             ▼               ▼
         主 Agent 接收结果 / 通知
```

## 核心概念

| 概念 | 说明 |
|------|------|
| **AgentId** | 8 位十六进制短随机字符串，全局唯一标识一个 Agent |
| **AgentConfig** | Agent 的不可变配置（ID、worktree 路径、是否 fork、超时阈值） |
| **SubAgent** | 子 Agent 运行时对象，持有独立的 QueryEngine 和 QueryState |
| **AgentTool** | LLM 创建子 Agent 的工具入口，支持 sync / async / fork 三种模式 |
| **AgentManager** | 管理所有活跃子 Agent 的创建、注册、清理 |
| **TaskService** | 统一的异步任务追踪系统，管理 local_agent 和 local_bash 两种任务 |

## 文档索引

| 文档 | 内容 |
|------|------|
| [agent.md](./agent.md) | Agent 抽象、生命周期、AgentTool、AgentManager |
| [task.md](./task.md) | 统一 Task 系统（local_agent / local_bash）|
| [infrastructure.md](./infrastructure.md) | Worktree 隔离 + 消息同步 + output 持久化 |

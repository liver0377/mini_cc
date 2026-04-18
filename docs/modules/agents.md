# 多智能体系统 (agents)

多智能体系统负责子智能体的创建、调度、执行和生命周期管理。子智能体在独立的工作区（git worktree）中运行，支持并行执行，失败时通过快照机制回滚。

## 模块结构

```
runtime/agents/
├── manager.py      # AgentManager — 智能体管理器
├── sub_agent.py    # SubAgent — 子智能体运行单元
├── dispatcher.py   # AgentDispatcher — 调度器
├── bus.py          # AgentEventBus — 生命周期事件总线
└── snapshot.py     # SnapshotService — 文件快照服务
```

## 架构图

```
┌───────────────────────────────────────────────────────────┐
│                     AgentManager                           │
│                                                           │
│  · 创建智能体（AgentConfig → SubAgent）                     │
│  · 管理工作区隔离（git worktree）                           │
│  · 检测作用域重叠                                           │
│  · 构建每个智能体的独立 EngineContext                       │
│  · 版本戳管理                                              │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │                   SubAgent 集合                      │  │
│  │                                                     │  │
│  │  ┌───────────┐  ┌───────────┐  ┌───────────┐      │  │
│  │  │ SubAgent A│  │ SubAgent B│  │ SubAgent C│      │  │
│  │  │ (写入型)   │  │ (只读型)   │  │ (只读型)   │      │  │
│  │  │ worktree-1│  │ worktree-2│  │ worktree-3│      │  │
│  │  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘      │  │
│  │        │              │              │              │  │
│  └────────┼──────────────┼──────────────┼──────────────┘  │
│           │              │              │                  │
│           ▼              ▼              ▼                  │
│  ┌──────────────────────────────────────────────────────┐ │
│  │              AgentEventBus（事件总线）                  │ │
│  │                                                      │ │
│  │  AgentStartEvent / AgentTextDelta / AgentToolCall    │ │
│  │  AgentToolResult / AgentCompletionEvent              │ │
│  └──────────────────────────────────────────────────────┘ │
│           │              │              │                  │
│           ▼              ▼              ▼                  │
│  ┌──────────────────────────────────────────────────────┐ │
│  │          AgentCompletionCoordinator                   │ │
│  │  · 排水完成事件队列                                     │ │
│  │  · 收集所有智能体完成结果                                │ │
│  │  · 构建智能体摘要                                       │ │
│  └──────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────┐
│                  SnapshotService                          │
│  · 文件级快照（执行前拍摄）                                  │
│  · 智能体失败时回滚                                        │
│  · 成功时清理快照                                          │
└───────────────────────────────────────────────────────────┘
```

## 核心组件

### AgentManager

智能体管理器，是子智能体系统的入口：

| 职责 | 说明 |
|------|------|
| 创建智能体 | 根据 `AgentConfig` 创建 `SubAgent` 实例 |
| 工作区隔离 | 为每个智能体创建独立的 git worktree |
| 作用域检测 | 检查多个智能体的文件作用域是否重叠，防止冲突 |
| 独立引擎 | 为每个智能体构建独立的 `EngineContext` |
| 版本管理 | 维护版本戳，确保一致性 |

**智能体创建流程：**

```
AgentManager.dispatch(config)
   │
   ├── 1. 解析 AgentConfig（任务描述、作用域、角色）
   │
   ├── 2. 检测作用域重叠
   │       └── 若与其他活跃智能体冲突 → 报错或等待
   │
   ├── 3. 创建 git worktree
   │       └── 从当前 HEAD 创建独立工作目录
   │
   ├── 4. 构建独立 EngineContext
   │       ├── 独立的 SystemPromptBuilder
   │       ├── 独立的 ToolRegistry（限制作用域）
   │       ├── 独立的 QueryEngine
   │       └── 独立的 ExecutionPolicy（限定路径）
   │
   ├── 5. 创建 SubAgent 实例
   │
   └── 6. 启动执行
           ├── 前台：同步等待完成
           └── 后台：异步运行，通过 EventBus 通知
```

### SubAgent

子智能体运行单元，封装单个智能体的完整执行过程：

| 属性 | 说明 |
|------|------|
| 标识 | `AgentId`（自动生成） |
| 配置 | `AgentConfig`（任务、作用域、角色） |
| 状态 | `AgentStatus`（pending / running / completed / failed / cancelled） |
| 预算 | `AgentBudget`（token 限制、轮次限制） |
| 工作区 | git worktree 路径 |

**执行流程：**

```
SubAgent.run()
   │
   ├── 1. 状态: pending → running
   │
   ├── 2. 发射 AgentStartEvent
   │
   ├── 3. SnapshotService 拍摄快照
   │
   ├── 4. 运行 QueryEngine（Agent Loop）
   │       ├── 收集文本输出
   │       ├── 收集工具调用和结果
   │       └── 发射事件到 EventBus
   │
   ├── 5. 成功:
   │       ├── 状态 → completed
   │       ├── 写入输出文件
   │       ├── 清理快照
   │       └── 发射 AgentCompletionEvent
   │
   └── 6. 失败:
           ├── 状态 → failed
           ├── SnapshotService 回滚
           └── 发射 AgentCompletionEvent（含错误）
```

### AgentDispatcher

智能体调度器，负责批量调度和预算追踪：

```
AgentDispatcher
├── dispatch()               # 调度单个智能体
│   ├── 预算检查
│   └── 传递给 AgentManager
│
├── dispatch_batch_readonly() # 批量调度只读智能体
│   ├── 并行创建多个智能体
│   └── 全部异步执行
│
└── 预算追踪
    ├── 已使用预算
    └── 剩余预算
```

### AgentEventBus

基于 `asyncio.Queue` 的异步事件总线：

```
AgentEventBus
├── publish(event)    # 发布生命周期事件
├── drain()           # 排水所有待处理事件
└── 事件类型
    ├── AgentStartEvent
    ├── AgentTextDeltaEvent
    ├── AgentToolCallEvent
    ├── AgentToolResultEvent
    └── AgentCompletionEvent
```

### SnapshotService

文件级快照服务，用于智能体失败时的回滚：

```
SnapshotService
├── 拍摄快照
│   ├── 记录 worktree 中所有文件的当前状态
│   └── 存储为快照数据
│
├── 回滚
│   ├── 智能体失败时触发
│   └── 将所有文件恢复到快照状态
│
└── 清理
    └── 智能体成功后删除快照数据
```

## 智能体类型

| 类型 | 执行方式 | 工作区 | 作用域限制 | 典型用途 |
|------|----------|--------|-----------|----------|
| 写入型 | 前台同步 | 独立 worktree | 指定路径 | 代码编辑、文件创建 |
| 只读型 | 后台异步 | 独立 worktree | 只读 | 代码分析、搜索、审查 |

## 作用域隔离机制

```
┌────────────────────────────────────────────┐
│              主工作区（main tree）            │
│                                            │
│  ┌─────────────┐  ┌──────────────────┐    │
│  │  Agent A     │  │  Agent B          │    │
│  │  worktree-1  │  │  worktree-2       │    │
│  │  scope:      │  │  scope:           │    │
│  │  src/auth/   │  │  src/api/         │    │
│  └─────────────┘  └──────────────────┘    │
│                                            │
│  作用域不重叠 ✓ → 可并行执行                  │
│                                            │
│  ┌─────────────┐  ┌──────────────────┐    │
│  │  Agent C     │  │  Agent D          │    │
│  │  worktree-3  │  │  worktree-4       │    │
│  │  scope:      │  │  scope:           │    │
│  │  src/core/   │  │  src/core/db.py   │    │
│  └─────────────┘  └──────────────────┘    │
│                                            │
│  作用域重叠 ✗ → 需串行执行或报错              │
└────────────────────────────────────────────┘
```

## 与主引擎的交互

```
QueryEngine（主引擎）
   │
   ├── 检测到 agent 工具调用
   │
   ▼
AgentTool.execute()
   │
   ├── 单个智能体
   │   └── AgentManager.dispatch() → SubAgent 运行 → 收集结果
   │
   └── 批量计划（PlanAgentsTool 生成）
       └── AgentDispatcher.dispatch_batch_readonly()
           ├── Agent A（异步）
           ├── Agent B（异步）
           └── Agent C（异步）
                   │
                   ▼
           AgentCompletionCoordinator.drain()
                   │
                   ▼
           收集所有完成事件 → 构建汇总结果
```

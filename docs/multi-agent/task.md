# Task 系统

## 概述

统一 Task 系统用于追踪所有异步执行单元，为 Agent 和后台命令提供一致的任务管理基础设施。

## Task 类型

| 类型 | 说明 |
|------|------|
| local_agent | 异步子 Agent 对应的 Task |
| local_bash | 后台执行的 bash 命令对应的 Task |

未来可扩展：remote_agent（远程 Agent）、scheduled_task（定时任务）等。

## 存储结构

每个 Task 是一个独立的 JSON 文件，路径格式：

```
~/.local/share/mini_cc/tasks/<task_list_id>/<id>.json
```

- **task_list_id**：标识一个任务列表，对应一次对话会话（主 Agent 的 session）
- **id**：任务列表内自增整数，从 1 开始

## Task 字段

### 通用字段

| 字段 | 说明 |
|------|------|
| id | 自增整数（1, 2, 3...），由 TaskService 自动分配 |
| type | 任务类型（local_agent / local_bash） |
| subject | 祈使句标题（如 "Fix auth bug"） |
| description | 详细描述 |
| active_form | 进行时形式（如 "Fixing auth bug"），用于 spinner 展示 |
| owner | 认领该任务的 Agent ID |
| status | 任务状态（见生命周期） |
| output_path | 结果输出文件路径 |
| blocks | 此任务阻塞了哪些任务的 ID 列表 |
| blocked_by | 哪些任务阻塞了此任务的 ID 列表 |
| metadata | 任意附加数据 |

### local_agent 特有字段

| 字段 | 说明 |
|------|------|
| agent_id | 关联的子 Agent ID |
| prompt | 子 Agent 的任务描述 |
| is_fork | 是否为 Forked Agent |
| parent_agent_id | Fork 的父 Agent ID |

### local_bash 特有字段

| 字段 | 说明 |
|------|------|
| command | 后台执行的 bash 命令 |

## 依赖关系

任务间通过 blocks / blocked_by 双向字段表达依赖，两个字段必须保持一致：

```
task_1.blocks     = [2]     →  任务 1 阻塞任务 2
task_2.blocked_by = [1]     →  任务 2 被任务 1 阻塞
```

创建和删除任务时，系统自动维护引用完整性——遍历全部任务，同步更新 blocks 和 blocked_by 中的引用。

## 并发控制

多 Agent 共享同一任务列表时，使用文件锁（fcntl.flock）防止竞态：

1. 获取任务文件锁，失败时指数退避重试
2. 读取 JSON → 校验状态 → 修改 → 写回
3. 释放锁

## 生命周期

```
  pending ──认领──→ in_progress ──完成──→ completed
    │                   │
    │                   └──失败──→ failed
    └───────取消────────────┴──→ cancelled
```

| 阶段 | 说明 |
|------|------|
| **创建** | 状态为 pending，写入 blocked_by 依赖 |
| **认领** | Agent 将状态改为 in_progress，填充 owner |
| **完成** | 状态改为 completed，系统自动解除下游任务的阻塞（从下游的 blocked_by 中移除本任务 ID） |
| **失败** | 状态改为 failed，在 metadata.error 中记录错误信息 |
| **取消** | 遍历清理 blocks 和 blocked_by 中对该任务的引用 |

## TaskService 操作

| 操作 | 说明 |
|------|------|
| create | 创建新任务，自动分配自增 ID |
| get | 按 ID 获取单个任务 |
| list_all | 列出当前任务列表中的所有任务 |
| update | 更新任务的任意字段 |
| claim | 认领任务（设置 owner + 状态改为 in_progress） |
| complete | 完成任务（状态改为 completed + 解除下游阻塞） |
| fail | 标记任务失败（记录错误信息） |
| cancel | 取消任务（清理所有引用关系） |
| get_ready_tasks | 返回所有无阻塞的 pending 任务（blocked_by 已全部满足） |

## 与 Multi-Agent 的集成

### local_agent Task

AgentTool 创建异步只读子 Agent 时，自动创建 local_agent 类型的 Task。子 Agent 完成后，通过回调将 Task 状态更新为 completed。

### local_bash Task

BashTool 设置为后台执行时，自动创建 local_bash 类型的 Task。命令执行完毕后更新 Task 状态。

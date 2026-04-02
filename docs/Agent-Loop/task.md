# Design

## 概述

本文档描述 Mini Claude Code 的任务管理系统。任务系统用于在多 Agent 协作场景下，分配、追踪和协调工作项。

## 存储结构

每个任务是一个独立的 JSON 文件，路径格式为：

    ~/.local/share/mini_cc/tasks/<task_list_id>/<id>.json

- `task_list_id`：标识一个任务列表，对应一次对话会话或一个 Agent 团队
- `id`：任务列表内自增的整数，从 1 开始

## 任务 Schema

```python
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class Task(BaseModel):
    id: int                                    # 自增整数（1, 2, 3...）
    subject: str                               # 祈使句标题（如 "Fix auth bug"）
    description: str                           # 详细描述
    active_form: str | None = None             # 进行时形式（如 "Fixing auth bug"），用于 spinner 展示
    owner: str | None = None                   # 认领该任务的 Agent ID
    status: TaskStatus = TaskStatus.PENDING
    blocks: list[int] = Field(default_factory=list)    # 此任务阻塞了哪些任务（即这些任务需等待本任务完成）
    blocked_by: list[int] = Field(default_factory=list)  # 哪些任务阻塞了此任务（即本任务需等待它们完成）
    metadata: dict[str, Any] = Field(default_factory=dict)  # 任意附加数据
```

## 依赖关系

任务间通过 `blocks` / `blocked_by` 双向字段表达依赖，两个字段必须保持一致：

```
task_1.blocks     = [2]    →  任务 1 完成前，任务 2 不能开始（1 阻塞 2）
task_2.blocked_by = [1]    →  任务 2 必须等任务 1 完成（2 被 1 阻塞）
```

创建和删除任务时，系统自动维护引用完整性（遍历全部任务，同步更新 `blocks` 和 `blocked_by` 中的引用）。

## 并发控制

多 Agent 共享同一任务列表时，使用文件锁（`fcntl.flock`）防止竞态条件：

1. 获取任务文件锁，失败时采用指数退避重试
2. 读取 JSON → 校验状态 → 修改 → 写回
3. 释放锁

## 任务生命周期

```
  pending ──认领──→ in_progress ──完成──→ completed
    │                   │
    └───────删除────────┘
```

1. **创建** — 状态为 `pending`，写入 `blocked_by` 依赖
2. **认领** — Agent 将状态改为 `in_progress`，填充 `owner`
3. **完成** — 状态改为 `completed`，系统自动解除下游任务的阻塞（从下游的 `blocked_by` 中移除本任务 ID）
4. **删除** — 遍历全部任务，清理 `blocks` 和 `blocked_by` 中对该 ID 的引用，删除文件

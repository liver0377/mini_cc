from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskType(StrEnum):
    LOCAL_AGENT = "local_agent"
    LOCAL_BASH = "local_bash"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    id: int = -1
    type: TaskType
    subject: str
    description: str
    active_form: str | None = None
    owner: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    output_path: str | None = None
    blocks: list[int] = Field(default_factory=list)
    blocked_by: list[int] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    agent_id: str | None = None
    prompt: str | None = None
    is_fork: bool = False
    parent_agent_id: str | None = None

    command: str | None = None

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class AgentStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    BACKGROUND_RUNNING = "background_running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def generate_agent_id() -> str:
    return secrets.token_hex(4)


class AgentConfig(BaseModel):
    agent_id: str
    worktree_path: str
    is_fork: bool = False
    parent_agent_id: str | None = None
    timeout_seconds: int = 120

    @property
    def worktree(self) -> Path:
        return Path(self.worktree_path)


@dataclass(frozen=True)
class AgentId:
    value: str

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AgentId):
            return self.value == other.value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)

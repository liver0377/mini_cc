from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from mini_cc.models.message import Message


class QueryState(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    turn_count: int = 0


@dataclass
class ToolCallSummary:
    tool_call_id: str
    name: str
    success: bool
    output_length: int


@dataclass
class TurnRecord:
    turn: int
    text_length: int = 0
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    elapsed_ms: float = 0.0


@dataclass
class QueryTracking:
    turn: int = 0
    history: list[TurnRecord] = field(default_factory=list)

    def record_turn(self, record: TurnRecord) -> None:
        self.history.append(record)
        self.turn = record.turn + 1

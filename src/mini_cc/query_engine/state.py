from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class Message(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class TextDelta:
    content: str


@dataclass
class ToolCallStart:
    tool_call_id: str
    name: str


@dataclass
class ToolCallDelta:
    tool_call_id: str
    arguments_json_delta: str


@dataclass
class ToolCallEnd:
    tool_call_id: str


@dataclass
class ToolResultEvent:
    tool_call_id: str
    name: str
    output: str
    success: bool


Event = TextDelta | ToolCallStart | ToolCallDelta | ToolCallEnd | ToolResultEvent


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


@dataclass
class _ToolCallBuffer:
    id: str = ""
    name: str = ""
    arguments: str = ""


def collect_tool_calls(events: list[Event]) -> list[ToolCall]:
    buffers: dict[str, _ToolCallBuffer] = {}
    order: list[str] = []

    for event in events:
        if isinstance(event, ToolCallStart):
            buffers[event.tool_call_id] = _ToolCallBuffer(id=event.tool_call_id, name=event.name)
            order.append(event.tool_call_id)
        elif isinstance(event, ToolCallDelta):
            existing = buffers.get(event.tool_call_id)
            if existing is not None:
                existing.arguments += event.arguments_json_delta

    result: list[ToolCall] = []
    for tool_call_id in order:
        buf = buffers[tool_call_id]
        result.append(ToolCall(id=buf.id, name=buf.name, arguments=buf.arguments))
    return result

from __future__ import annotations

from dataclasses import dataclass

from mini_cc.models.message import ToolCall


class ContextLengthExceededError(Exception):
    pass


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


@dataclass
class AgentStartEvent:
    agent_id: str
    task_id: int
    prompt: str


@dataclass
class AgentTextDeltaEvent:
    agent_id: str
    content: str


@dataclass
class AgentToolCallEvent:
    agent_id: str
    tool_name: str


@dataclass
class AgentToolResultEvent:
    agent_id: str
    tool_name: str
    success: bool
    output_preview: str


@dataclass
class AgentHeartbeatEvent:
    agent_id: str
    task_id: int
    elapsed_seconds: int
    status: str = "running"


@dataclass
class AgentCompletionEvent:
    agent_id: str
    task_id: int
    success: bool
    output: str
    output_path: str
    base_version_stamp: str = ""
    completed_version_stamp: str = ""
    is_stale: bool = False


@dataclass
class CompactOccurred:
    reason: str


Event = (
    TextDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | ToolResultEvent
    | CompactOccurred
    | AgentStartEvent
    | AgentTextDeltaEvent
    | AgentToolCallEvent
    | AgentToolResultEvent
    | AgentHeartbeatEvent
    | AgentCompletionEvent
)


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

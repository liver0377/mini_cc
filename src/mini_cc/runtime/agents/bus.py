from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class AgentLifecycleEvent:
    event_type: str
    agent_id: str
    source_step_id: str | None = None
    work_item_id: str | None = None
    readonly: bool = False
    scope_paths: list[str] | None = None
    success: bool | None = None
    output_preview: str = ""
    output_path: str | None = None
    is_stale: bool = False
    base_version_stamp: str = ""
    completed_version_stamp: str = ""
    termination_reason: str | None = None
    heartbeat_at: str | None = None
    heartbeat_elapsed_seconds: int | None = None
    heartbeat_status: str = ""


class AgentEventBus:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[AgentLifecycleEvent] = asyncio.Queue()

    async def publish(self, event: AgentLifecycleEvent) -> None:
        await self._queue.put(event)

    def publish_nowait(self, event: AgentLifecycleEvent) -> None:
        self._queue.put_nowait(event)

    def drain(self) -> list[AgentLifecycleEvent]:
        events: list[AgentLifecycleEvent] = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

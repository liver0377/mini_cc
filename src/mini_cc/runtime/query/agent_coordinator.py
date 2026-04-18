from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable

from mini_cc.models import (
    AgentCompletionEvent,
    Event,
    Message,
    MessageSource,
    QueryState,
    Role,
)


class AgentCompletionCoordinator:
    def __init__(
        self,
        completion_queue: asyncio.Queue[AgentCompletionEvent] | None = None,
        agent_event_queue: asyncio.Queue[Event] | None = None,
        active_agents_fn: Callable[[], int] | None = None,
        is_interrupted_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._completion_queue = completion_queue
        self._agent_event_queue = agent_event_queue
        self._active_agents_fn = active_agents_fn
        self._is_interrupted_fn = is_interrupted_fn or (lambda: False)

    @property
    def has_completion_queue(self) -> bool:
        return self._completion_queue is not None

    @property
    def has_active_agents(self) -> bool:
        return self._active_agents_fn is not None and self._active_agents_fn() > 0

    async def drain_completions(self) -> AsyncGenerator[AgentCompletionEvent, None]:
        if self._completion_queue is None:
            return
        while not self._completion_queue.empty():
            try:
                evt = self._completion_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield evt

    async def drain_agent_events(self) -> AsyncGenerator[Event, None]:
        if self._agent_event_queue is None:
            return
        while not self._agent_event_queue.empty():
            try:
                event = self._agent_event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield event

    async def drain_all(self) -> AsyncGenerator[Event, None]:
        async for notification in self.drain_completions():
            yield notification
        async for event in self.drain_agent_events():
            yield event

    async def collect_all_completions(self) -> tuple[list[AgentCompletionEvent], list[Event]]:
        assert self._completion_queue is not None
        assert self._active_agents_fn is not None

        completions: list[AgentCompletionEvent] = []
        waiting_events: list[Event] = []
        async for evt in self.drain_completions():
            completions.append(evt)
        async for event in self.drain_agent_events():
            waiting_events.append(event)

        while self._active_agents_fn() > 0:
            if self._is_interrupted_fn():
                break
            try:
                evt = await asyncio.wait_for(self._completion_queue.get(), timeout=1.0)
            except TimeoutError:
                async for event in self.drain_agent_events():
                    waiting_events.append(event)
                continue
            completions.append(evt)
            async for event in self.drain_agent_events():
                waiting_events.append(event)

        return completions, waiting_events

    def build_agent_summary(self, completions: list[AgentCompletionEvent]) -> str:
        return _build_agent_summary(completions)

    def inject_summary(self, state: QueryState, completions: list[AgentCompletionEvent]) -> None:
        summary = self.build_agent_summary(completions)
        state.messages.append(Message(role=Role.USER, content=summary, source=MessageSource.AGENT_SUMMARY))


def _build_agent_summary(completions: list[AgentCompletionEvent]) -> str:
    summary_parts: list[str] = []
    for c in completions:
        status_label = "成功" if c.success else "失败"
        stale_label = " [结果可能过期]" if c.is_stale else ""
        version_lines = ""
        if c.base_version_stamp or c.completed_version_stamp:
            version_lines = (
                f"\n\nbase_version: {c.base_version_stamp or '(无)'}"
                f"\ncompleted_version: {c.completed_version_stamp or '(无)'}"
            )
        summary_parts.append(
            f"## 子 Agent {c.agent_id} (Task #{c.task_id}) - {status_label}{stale_label}\n\n{c.output}{version_lines}"
        )
    summary = "\n\n---\n\n".join(summary_parts)
    return f"以下是之前启动的后台只读子 Agent 的完成结果。\n请基于这些结果，继续回复用户的原始问题。\n\n{summary}"

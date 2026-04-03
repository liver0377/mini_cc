from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from mini_cc.context.tool_use import ToolUseContext
from mini_cc.query_engine.state import (
    AgentCompletionNotificationEvent,
    Event,
    Message,
    QueryState,
    QueryTracking,
    Role,
    TextDelta,
    ToolCall,
    ToolCallSummary,
    ToolResultEvent,
    TurnRecord,
    collect_tool_calls,
)
from mini_cc.task.models import AgentCompletionEvent

StreamFn = Callable[
    [list[Message], list[dict[str, Any]]],
    AsyncGenerator[Event, None],
]


class QueryEngine:
    def __init__(
        self,
        stream_fn: StreamFn,
        tool_use_ctx: ToolUseContext,
        completion_queue: asyncio.Queue[AgentCompletionEvent] | None = None,
    ) -> None:
        self._stream_fn = stream_fn
        self._tool_use_ctx = tool_use_ctx
        self._completion_queue = completion_queue
        self.state: QueryState | None = None

    async def submit_message(self, prompt: str, state: QueryState | None = None) -> AsyncGenerator[Event, None]:
        if state is None:
            state = QueryState()
        state.messages.append(Message(role=Role.USER, content=prompt))
        self.state = state
        async for event in self._query_loop(state):
            yield event

    async def _drain_completions(self) -> AsyncGenerator[AgentCompletionNotificationEvent, None]:
        if self._completion_queue is None:
            return
        while not self._completion_queue.empty():
            try:
                evt = self._completion_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield AgentCompletionNotificationEvent(
                agent_id=evt.agent_id,
                task_id=evt.task_id,
                success=evt.success,
                output=evt.output,
                output_path=str(evt.output_path),
            )

    async def _query_loop(self, state: QueryState) -> AsyncGenerator[Event, None]:
        tracking = QueryTracking()

        while True:
            if self._tool_use_ctx.is_interrupted:
                break

            async for notification in self._drain_completions():
                yield notification

            schemas = self._tool_use_ctx.get_tool_schemas()
            self._tool_use_ctx.trace("stream_start", turn=tracking.turn)

            t0 = time.monotonic()
            turn_events: list[Event] = []
            async for event in self._stream_fn(state.messages, schemas):
                yield event
                turn_events.append(event)

            tool_calls = collect_tool_calls(turn_events)
            if not tool_calls:
                break

            allowed: list[ToolCall] = []
            for tc in tool_calls:
                if self._tool_use_ctx.check_permission(tc.name):
                    allowed.append(tc)
                else:
                    yield ToolResultEvent(
                        tool_call_id=tc.id,
                        name=tc.name,
                        output="Permission denied",
                        success=False,
                    )

            if not allowed:
                break

            tool_results: list[ToolResultEvent] = []
            async for result in self._tool_use_ctx.execute(allowed):
                yield result
                tool_results.append(result)

            assistant_content = _extract_text_content(turn_events)
            state.messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=assistant_content,
                    tool_calls=tool_calls,
                )
            )
            for tr in tool_results:
                state.messages.append(
                    Message(
                        role=Role.TOOL,
                        content=tr.output,
                        tool_call_id=tr.tool_call_id,
                        name=tr.name,
                    )
                )

            elapsed_ms = (time.monotonic() - t0) * 1000
            record = TurnRecord(
                turn=tracking.turn,
                text_length=len(assistant_content),
                tool_calls=[
                    ToolCallSummary(
                        tool_call_id=tr.tool_call_id,
                        name=tr.name,
                        success=tr.success,
                        output_length=len(tr.output),
                    )
                    for tr in tool_results
                ],
                elapsed_ms=elapsed_ms,
            )
            tracking.record_turn(record)

            state.turn_count += 1
            self._tool_use_ctx.trace("stream_end", turn=tracking.turn)


def _extract_text_content(events: list[Event]) -> str:
    parts: list[str] = []
    for event in events:
        if isinstance(event, TextDelta):
            parts.append(event.content)
    return "".join(parts)

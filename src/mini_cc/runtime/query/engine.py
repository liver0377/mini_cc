from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from mini_cc.context.tool_use import ToolUseContext
from mini_cc.models import (
    ContextLengthExceededError,
    Event,
    Message,
    MessageSource,
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
from mini_cc.runtime.query.agent_coordinator import AgentCompletionCoordinator
from mini_cc.runtime.query.compaction import CompactionController

StreamFn = Callable[
    [list[Message], list[dict[str, Any]]],
    AsyncGenerator[Event, None],
]

PostTurnHook = Callable[[QueryState], Awaitable[None]]


_DEFAULT_MAX_TURNS = 50


class QueryEngine:
    def __init__(
        self,
        stream_fn: StreamFn,
        tool_use_ctx: ToolUseContext,
        completion_queue: asyncio.Queue[Any] | None = None,
        agent_event_queue: asyncio.Queue[Any] | None = None,
        post_turn_hook: PostTurnHook | None = None,
        model: str = "",
        active_agents_fn: Callable[[], int] | None = None,
        compact_fn: Any | None = None,
        should_compact_fn: Any | None = None,
        replace_summary_fn: Any | None = None,
        max_turns: int = _DEFAULT_MAX_TURNS,
    ) -> None:
        self._stream_fn = stream_fn
        self._tool_use_ctx = tool_use_ctx
        self._post_turn_hook = post_turn_hook
        self._model = model
        self._max_turns = max_turns
        self.state: QueryState | None = None

        self._compaction = CompactionController(
            compact_fn=compact_fn,
            should_compact_fn=should_compact_fn,
            replace_summary_fn=replace_summary_fn,
        )

        self._coordinator = AgentCompletionCoordinator(
            completion_queue=completion_queue,
            agent_event_queue=agent_event_queue,
            active_agents_fn=active_agents_fn,
            is_interrupted_fn=lambda: tool_use_ctx.is_interrupted,
        )

    async def submit_message(self, prompt: str, state: QueryState | None = None) -> AsyncGenerator[Event, None]:
        if state is None:
            state = QueryState()
        state.messages.append(Message(role=Role.USER, content=prompt, source=MessageSource.USER))
        self.state = state
        async for event in self._query_loop(state):
            yield event

    async def _query_loop(self, state: QueryState) -> AsyncGenerator[Event, None]:
        tracking = QueryTracking()
        has_attempted_reactive = False

        while tracking.turn < self._max_turns:
            if self._tool_use_ctx.is_interrupted:
                break

            async for event in self._coordinator.drain_all():
                yield event

            if self._compaction.should_compact(state.messages):
                compact_event = await self._compaction.compact(state, reason="auto")
                if compact_event is not None:
                    yield compact_event
                has_attempted_reactive = False

            schemas = self._tool_use_ctx.get_tool_schemas()
            self._tool_use_ctx.trace("stream_start", turn=tracking.turn)

            t0 = time.monotonic()
            turn_events: list[Event] = []
            try:
                async for event in self._stream_fn(state.messages, schemas):
                    yield event
                    turn_events.append(event)
            except ContextLengthExceededError:
                if has_attempted_reactive:
                    raise
                has_attempted_reactive = True
                compact_event = await self._compaction.compact(state, reason="reactive")
                if compact_event is not None:
                    yield compact_event
                continue

            tool_calls = collect_tool_calls(turn_events)
            if not tool_calls:
                assistant_content = _extract_text_content(turn_events)

                if self._coordinator.has_completion_queue and self._coordinator.has_active_agents:
                    state.messages.append(Message(role=Role.ASSISTANT, content=assistant_content))

                    async for event in self._coordinator.drain_agent_events():
                        yield event

                    completions, waiting_events = await self._coordinator.collect_all_completions()
                    for event in waiting_events:
                        yield event
                    for evt in completions:
                        yield evt

                    if completions:
                        self._coordinator.inject_summary(state, completions)
                        has_attempted_reactive = False
                        continue

                state.messages.append(Message(role=Role.ASSISTANT, content=assistant_content))
                record = TurnRecord(
                    turn=tracking.turn,
                    text_length=len(assistant_content),
                    elapsed_ms=(time.monotonic() - t0) * 1000,
                )
                tracking.record_turn(record)
                state.turn_count += 1
                self._tool_use_ctx.trace("stream_end", turn=tracking.turn)

                if self._post_turn_hook is not None:
                    await self._post_turn_hook(state)

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
                async for event in self._coordinator.drain_agent_events():
                    yield event
                yield result
                tool_results.append(result)

            async for event in self._coordinator.drain_agent_events():
                yield event

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

            if self._post_turn_hook is not None:
                await self._post_turn_hook(state)

        async for notification in self._coordinator.drain_completions():
            yield notification
        async for event in self._coordinator.drain_agent_events():
            yield event


def _extract_text_content(events: list[Event]) -> str:
    parts: list[str] = []
    for event in events:
        if isinstance(event, TextDelta):
            parts.append(event.content)
    return "".join(parts)

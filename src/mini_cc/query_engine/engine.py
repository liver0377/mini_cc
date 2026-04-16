from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from mini_cc.compression.compressor import (
    ContextLengthExceededError,
    compress_messages,
    replace_with_summary,
    should_auto_compact,
)
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.models import (
    AgentCompletionEvent,
    CompactOccurred,
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

StreamFn = Callable[
    [list[Message], list[dict[str, Any]]],
    AsyncGenerator[Event, None],
]

PostTurnHook = Callable[[QueryState], Awaitable[None]]


class QueryEngine:
    def __init__(
        self,
        stream_fn: StreamFn,
        tool_use_ctx: ToolUseContext,
        completion_queue: asyncio.Queue[AgentCompletionEvent] | None = None,
        agent_event_queue: asyncio.Queue[Event] | None = None,
        post_turn_hook: PostTurnHook | None = None,
        model: str = "",
        active_agents_fn: Callable[[], int] | None = None,
    ) -> None:
        self._stream_fn = stream_fn
        self._tool_use_ctx = tool_use_ctx
        self._completion_queue = completion_queue
        self._agent_event_queue = agent_event_queue
        self._post_turn_hook = post_turn_hook
        self._model = model
        self._active_agents_fn = active_agents_fn
        self.state: QueryState | None = None

    async def submit_message(self, prompt: str, state: QueryState | None = None) -> AsyncGenerator[Event, None]:
        if state is None:
            state = QueryState()
        state.messages.append(Message(role=Role.USER, content=prompt))
        self.state = state
        async for event in self._query_loop(state):
            yield event

    async def _drain_completions(self) -> AsyncGenerator[AgentCompletionEvent, None]:
        if self._completion_queue is None:
            return
        while not self._completion_queue.empty():
            try:
                evt = self._completion_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield evt

    async def _drain_agent_events(self) -> AsyncGenerator[Event, None]:
        if self._agent_event_queue is None:
            return
        while not self._agent_event_queue.empty():
            try:
                event = self._agent_event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            yield event

    async def _query_loop(self, state: QueryState) -> AsyncGenerator[Event, None]:
        tracking = QueryTracking()
        has_attempted_reactive = False

        while True:
            if self._tool_use_ctx.is_interrupted:
                break

            async for notification in self._drain_completions():
                yield notification
            async for event in self._drain_agent_events():
                yield event

            # Phase 1: auto compact
            if should_auto_compact(state.messages, self._model):
                summary = await compress_messages(state.messages, self._stream_fn, self._model)
                replace_with_summary(state, summary)
                yield CompactOccurred(reason="auto")
                has_attempted_reactive = False

            schemas = self._tool_use_ctx.get_tool_schemas()
            self._tool_use_ctx.trace("stream_start", turn=tracking.turn)

            t0 = time.monotonic()
            turn_events: list[Event] = []
            try:
                # 调用 LLM 流式推理，实时 yield 事件给 UI，同时收集到 turn_events
                async for event in self._stream_fn(state.messages, schemas):
                    yield event
                    turn_events.append(event)
            except ContextLengthExceededError:
                # 被动压缩：LLM API 因上下文超长拒绝请求时的补救措施
                # has_attempted_reactive 确保最多补救一次，避免无限循环
                if has_attempted_reactive:
                    raise
                has_attempted_reactive = True
                # 用 stream_fn 调一次 LLM 生成摘要，替换历史消息（保留 system message）
                summary = await compress_messages(state.messages, self._stream_fn, self._model)
                replace_with_summary(state, summary)
                yield CompactOccurred(reason="reactive")
                # 回到 while True 顶部，用压缩后的 messages 重新调 LLM
                continue

            tool_calls = collect_tool_calls(turn_events)
            if not tool_calls:
                assistant_content = _extract_text_content(turn_events)

                if self._should_wait_for_agents():
                    state.messages.append(Message(role=Role.ASSISTANT, content=assistant_content))

                    async for event in self._drain_agent_events():
                        yield event

                    completions = await self._collect_all_completions()
                    for evt in completions:
                        yield evt

                    if completions:
                        summary = _build_agent_summary(completions)
                        state.messages.append(Message(role=Role.USER, content=summary))
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
                # 每执行完一个 tool 后排空 agent_event_queue，确保后台 readonly sub-agent
                # 的实时事件（工具调用/结果）不会因主循环长时间占用而饥饿，
                # 从而在 UI 上实现主 agent 与子 agent 事件的交错展示。
                async for event in self._drain_agent_events():
                    yield event
                yield result
                tool_results.append(result)

            # 兜底排空，防止最后一批后台事件丢失
            async for event in self._drain_agent_events():
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

        async for notification in self._drain_completions():
            yield notification
        async for event in self._drain_agent_events():
            yield event

    def _should_wait_for_agents(self) -> bool:
        return (
            self._completion_queue is not None and self._active_agents_fn is not None and self._active_agents_fn() > 0
        )

    async def _collect_all_completions(self) -> list[AgentCompletionEvent]:
        assert self._completion_queue is not None
        assert self._active_agents_fn is not None

        completions: list[AgentCompletionEvent] = []
        async for evt in self._drain_completions():
            completions.append(evt)

        while self._active_agents_fn() > 0:
            if self._tool_use_ctx.is_interrupted:
                break
            try:
                evt = await asyncio.wait_for(self._completion_queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            completions.append(evt)
            async for event in self._drain_agent_events():
                pass

        return completions


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


def _extract_text_content(events: list[Event]) -> str:
    parts: list[str] = []
    for event in events:
        if isinstance(event, TextDelta):
            parts.append(event.content)
    return "".join(parts)

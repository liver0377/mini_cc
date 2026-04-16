from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from mini_cc.context.tool_use import ToolUseContext
from mini_cc.models import (
    AgentCompletionEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    Message,
    QueryState,
    Role,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.query_engine.engine import QueryEngine


async def _stream_text_only(messages: list[Message], tools: list[dict[str, Any]]) -> AsyncGenerator[Event, None]:
    yield TextDelta(content="Hello!")


async def _stream_single_tool_then_text(
    messages: list[Message], tools: list[dict[str, Any]]
) -> AsyncGenerator[Event, None]:
    has_tool = any(m.role == Role.TOOL for m in messages)
    if has_tool:
        yield TextDelta(content="Done!")
        return

    yield TextDelta(content="Reading file...")
    yield ToolCallStart(tool_call_id="tc_1", name="file_read")
    yield ToolCallDelta(tool_call_id="tc_1", arguments_json_delta='{"file_path":"/tmp/a"}')
    yield ToolCallEnd(tool_call_id="tc_1")


async def _stream_two_turns_then_text(
    messages: list[Message], tools: list[dict[str, Any]]
) -> AsyncGenerator[Event, None]:
    tool_count = sum(1 for m in messages if m.role == Role.TOOL)
    if tool_count >= 2:
        yield TextDelta(content="All done!")
        return

    yield TextDelta(content=f"Turn {tool_count}...")
    yield ToolCallStart(tool_call_id=f"tc_{tool_count}", name="bash")
    yield ToolCallDelta(tool_call_id=f"tc_{tool_count}", arguments_json_delta='{"cmd":"ls"}')
    yield ToolCallEnd(tool_call_id=f"tc_{tool_count}")


async def _execute_ok(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
    for tc in tool_calls:
        yield ToolResultEvent(tool_call_id=tc.id, name=tc.name, output=f"result:{tc.name}", success=True)


def _make_ctx(
    *,
    check_permission: Any = None,
    is_interrupted: Any = None,
) -> ToolUseContext:
    return ToolUseContext(
        get_schemas=lambda: [{"name": "file_read"}, {"name": "bash"}],
        execute=_execute_ok,
        check_permission=check_permission,
        is_interrupted=is_interrupted,
    )


class TestQueryEngineTextOnly:
    async def test_yields_text_events(self) -> None:
        engine = QueryEngine(stream_fn=_stream_text_only, tool_use_ctx=_make_ctx())
        events = [e async for e in engine.submit_message("hi")]

        assert len(events) == 1
        assert isinstance(events[0], TextDelta)
        assert events[0].content == "Hello!"

    async def test_no_tool_execution(self) -> None:
        engine = QueryEngine(stream_fn=_stream_text_only, tool_use_ctx=_make_ctx())
        events = [e async for e in engine.submit_message("hi")]

        assert not any(isinstance(e, ToolResultEvent) for e in events)

    async def test_text_only_updates_state(self) -> None:
        engine = QueryEngine(stream_fn=_stream_text_only, tool_use_ctx=_make_ctx())
        _ = [e async for e in engine.submit_message("hi")]

        assert engine.state is not None
        assert engine.state.turn_count == 1
        assert len(engine.state.messages) == 2
        assert engine.state.messages[0].role == Role.USER
        assert engine.state.messages[1].role == Role.ASSISTANT
        assert engine.state.messages[1].content == "Hello!"


class TestQueryEngineSingleToolCall:
    async def test_yields_text_and_tool_events(self) -> None:
        engine = QueryEngine(stream_fn=_stream_single_tool_then_text, tool_use_ctx=_make_ctx())
        events = [e async for e in engine.submit_message("read file")]

        text_deltas = [e for e in events if isinstance(e, TextDelta)]
        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]

        assert len(text_deltas) >= 1
        assert len(tool_results) == 1
        assert tool_results[0].name == "file_read"
        assert tool_results[0].success is True

    async def test_state_updated(self) -> None:
        engine = QueryEngine(stream_fn=_stream_single_tool_then_text, tool_use_ctx=_make_ctx())
        _ = [e async for e in engine.submit_message("read file")]

        assert engine.state is not None
        assert engine.state.turn_count == 2


class TestQueryEngineMultiTurn:
    async def test_two_tool_turns(self) -> None:
        engine = QueryEngine(stream_fn=_stream_two_turns_then_text, tool_use_ctx=_make_ctx())
        events = [e async for e in engine.submit_message("run twice")]

        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 2

    async def test_turn_count_increments(self) -> None:
        engine = QueryEngine(stream_fn=_stream_two_turns_then_text, tool_use_ctx=_make_ctx())
        _ = [e async for e in engine.submit_message("run twice")]

        assert engine.state is not None
        assert engine.state.turn_count == 3


class TestQueryEnginePermissionDenied:
    async def test_denied_tool_yields_permission_error(self) -> None:
        ctx = _make_ctx(check_permission=lambda name: name == "file_read")
        engine = QueryEngine(
            stream_fn=_stream_two_turns_then_text,
            tool_use_ctx=ctx,
        )
        events = [e async for e in engine.submit_message("test")]

        denied = [e for e in events if isinstance(e, ToolResultEvent) and not e.success]
        assert len(denied) >= 1
        assert "Permission denied" in denied[0].output

    async def test_denied_tool_not_executed(self) -> None:
        executed: list[str] = []

        async def _stream_bash_then_text(
            messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncGenerator[Event, None]:
            has_tool = any(m.role == Role.TOOL for m in messages)
            if has_tool:
                yield TextDelta(content="Done!")
                return

            yield ToolCallStart(tool_call_id="tc_1", name="bash")
            yield ToolCallDelta(tool_call_id="tc_1", arguments_json_delta='{"cmd":"rm -rf /"}')
            yield ToolCallEnd(tool_call_id="tc_1")

        async def _execute_track(
            tool_calls: list[ToolCall],
        ) -> AsyncGenerator[ToolResultEvent, None]:
            for tc in tool_calls:
                executed.append(tc.name)
                yield ToolResultEvent(tool_call_id=tc.id, name=tc.name, output="ok", success=True)

        ctx = ToolUseContext(
            get_schemas=lambda: [{"name": "bash"}],
            execute=_execute_track,
            check_permission=lambda name: name != "bash",
        )
        engine = QueryEngine(stream_fn=_stream_bash_then_text, tool_use_ctx=ctx)
        events = [e async for e in engine.submit_message("test")]

        assert "bash" not in executed
        assert any(isinstance(e, ToolResultEvent) and not e.success for e in events)


class TestQueryEngineInterrupted:
    async def test_interrupted_stops_loop(self) -> None:
        interrupted = False

        def _check() -> bool:
            return interrupted

        ctx = _make_ctx(is_interrupted=_check)
        engine = QueryEngine(stream_fn=_stream_two_turns_then_text, tool_use_ctx=ctx)

        events: list[Event] = []
        async for event in engine.submit_message("test"):
            events.append(event)
            if len(events) == 1:
                interrupted = True

        assert len(events) >= 1


class TestQueryEngineStateMessages:
    async def test_messages_include_user_assistant_tool(self) -> None:
        engine = QueryEngine(stream_fn=_stream_single_tool_then_text, tool_use_ctx=_make_ctx())
        _ = [e async for e in engine.submit_message("read")]

        assert engine.state is not None
        messages = engine.state.messages
        assert messages[0].role == Role.USER
        assert messages[0].content == "read"

        assistant_msgs = [m for m in messages if m.role == Role.ASSISTANT]
        assert len(assistant_msgs) >= 1

        tool_msgs = [m for m in messages if m.role == Role.TOOL]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].name == "file_read"


class TestQueryEngineExistingState:
    async def test_reuses_existing_state(self) -> None:
        state = QueryState()
        state.messages.append(Message(role=Role.SYSTEM, content="You are helpful."))

        engine = QueryEngine(stream_fn=_stream_text_only, tool_use_ctx=_make_ctx())
        events = [e async for e in engine.submit_message("hi", state=state)]

        assert len(events) == 1
        assert isinstance(events[0], TextDelta)
        assert engine.state is state
        assert len(state.messages) == 3
        assert state.messages[0].role == Role.SYSTEM
        assert state.messages[1].role == Role.USER
        assert state.messages[1].content == "hi"
        assert state.messages[2].role == Role.ASSISTANT
        assert state.messages[2].content == "Hello!"

    async def test_multi_turn_accumulates_messages(self) -> None:
        engine = QueryEngine(stream_fn=_stream_single_tool_then_text, tool_use_ctx=_make_ctx())

        state = QueryState()
        _ = [e async for e in engine.submit_message("first", state=state)]
        assert len(state.messages) == 4
        assert state.messages[0].role == Role.USER
        assert state.messages[0].content == "first"

        _ = [e async for e in engine.submit_message("second", state=state)]
        assert len(state.messages) == 6
        assert state.messages[4].role == Role.USER
        assert state.messages[4].content == "second"
        assert state.messages[5].role == Role.ASSISTANT
        assert state.messages[5].content == "Done!"

    async def test_existing_state_with_tool_call(self) -> None:
        engine = QueryEngine(stream_fn=_stream_single_tool_then_text, tool_use_ctx=_make_ctx())

        state = QueryState()
        _ = [e async for e in engine.submit_message("read file", state=state)]

        user_msgs = [m for m in state.messages if m.role == Role.USER]
        assert len(user_msgs) == 1

        assistant_msgs = [m for m in state.messages if m.role == Role.ASSISTANT]
        assert len(assistant_msgs) == 2
        assert len(assistant_msgs[0].tool_calls) == 1
        assert assistant_msgs[1].content == "Done!"

        tool_msgs = [m for m in state.messages if m.role == Role.TOOL]
        assert len(tool_msgs) == 1
        assert state.turn_count == 2


class TestQueryEngineCompletionQueue:
    async def test_no_queue_no_notifications(self) -> None:
        engine = QueryEngine(stream_fn=_stream_text_only, tool_use_ctx=_make_ctx())
        events = [e async for e in engine.submit_message("hi")]

        assert not any(isinstance(e, AgentCompletionEvent) for e in events)

    async def test_empty_queue_no_notifications(self) -> None:
        queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            completion_queue=queue,
        )
        events = [e async for e in engine.submit_message("hi")]

        assert not any(isinstance(e, AgentCompletionEvent) for e in events)

    async def test_notification_yielded_before_text(self) -> None:
        queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        await queue.put(
            AgentCompletionEvent(
                agent_id="a3f7b2c1",
                task_id=1,
                success=True,
                output="agent done",
                output_path="/tmp/a3f7b2c1.output",
            )
        )
        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            completion_queue=queue,
        )
        events = [e async for e in engine.submit_message("hi")]

        notifications = [e for e in events if isinstance(e, AgentCompletionEvent)]
        assert len(notifications) == 1
        assert notifications[0].agent_id == "a3f7b2c1"
        assert notifications[0].success is True
        assert notifications[0].output == "agent done"
        assert notifications[0].output_path == "/tmp/a3f7b2c1.output"

        assert isinstance(events[0], AgentCompletionEvent)
        assert isinstance(events[1], TextDelta)

    async def test_multiple_notifications_drained(self) -> None:
        queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        for i in range(3):
            await queue.put(
                AgentCompletionEvent(
                    agent_id=f"agent{i:08x}",
                    task_id=i + 1,
                    success=True,
                    output=f"output {i}",
                    output_path=f"/tmp/agent{i:08x}.output",
                )
            )
        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            completion_queue=queue,
        )
        events = [e async for e in engine.submit_message("hi")]

        notifications = [e for e in events if isinstance(e, AgentCompletionEvent)]
        assert len(notifications) == 3

    async def test_notifications_on_multi_turn(self) -> None:
        queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        engine = QueryEngine(
            stream_fn=_stream_two_turns_then_text,
            tool_use_ctx=_make_ctx(),
            completion_queue=queue,
        )

        await queue.put(
            AgentCompletionEvent(
                agent_id="a3f7b2c1",
                task_id=1,
                success=True,
                output="done",
                output_path="/tmp/out.output",
            )
        )

        events = [e async for e in engine.submit_message("multi")]
        notifications = [e for e in events if isinstance(e, AgentCompletionEvent)]
        assert len(notifications) == 1

    async def test_failed_agent_notification(self) -> None:
        queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        await queue.put(
            AgentCompletionEvent(
                agent_id="deadbeef",
                task_id=5,
                success=False,
                output="error occurred",
                output_path="/tmp/deadbeef.output",
            )
        )
        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            completion_queue=queue,
        )
        events = [e async for e in engine.submit_message("hi")]

        notifications = [e for e in events if isinstance(e, AgentCompletionEvent)]
        assert len(notifications) == 1
        assert notifications[0].success is False
        assert notifications[0].output == "error occurred"


class TestQueryEngineAgentEventQueue:
    async def test_no_queue_no_events(self) -> None:
        engine = QueryEngine(stream_fn=_stream_text_only, tool_use_ctx=_make_ctx())
        events = [e async for e in engine.submit_message("hi")]
        assert not any(isinstance(e, AgentStartEvent) for e in events)

    async def test_empty_queue_no_events(self) -> None:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            agent_event_queue=queue,
        )
        events = [e async for e in engine.submit_message("hi")]
        assert not any(isinstance(e, (AgentStartEvent, AgentToolCallEvent, AgentToolResultEvent)) for e in events)

    async def test_agent_start_event_yielded(self) -> None:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        await queue.put(AgentStartEvent(agent_id="abc12345", task_id=1, prompt="test task"))
        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            agent_event_queue=queue,
        )
        events = [e async for e in engine.submit_message("hi")]

        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        assert len(start_events) == 1
        assert start_events[0].agent_id == "abc12345"
        assert start_events[0].task_id == 1
        assert start_events[0].prompt == "test task"

    async def test_agent_tool_call_and_result_events(self) -> None:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        await queue.put(AgentToolCallEvent(agent_id="abc12345", tool_name="file_read"))
        await queue.put(
            AgentToolResultEvent(agent_id="abc12345", tool_name="file_read", success=True, output_preview="ok")
        )
        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            agent_event_queue=queue,
        )
        events = [e async for e in engine.submit_message("hi")]

        tool_call_events = [e for e in events if isinstance(e, AgentToolCallEvent)]
        tool_result_events = [e for e in events if isinstance(e, AgentToolResultEvent)]
        assert len(tool_call_events) == 1
        assert len(tool_result_events) == 1
        assert tool_call_events[0].tool_name == "file_read"
        assert tool_result_events[0].success is True

    async def test_agent_events_drained_around_tool_execution(self) -> None:
        agent_queue: asyncio.Queue[Event] = asyncio.Queue()

        async def _execute_with_agent_events(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
            for tc in tool_calls:
                await agent_queue.put(AgentStartEvent(agent_id="abc12345", task_id=1, prompt="sub"))
                yield ToolResultEvent(tool_call_id=tc.id, name=tc.name, output="ok", success=True)

        ctx = ToolUseContext(
            get_schemas=lambda: [{"name": "file_read"}],
            execute=_execute_with_agent_events,
        )
        engine = QueryEngine(
            stream_fn=_stream_single_tool_then_text,
            tool_use_ctx=ctx,
            agent_event_queue=agent_queue,
        )
        events = [e async for e in engine.submit_message("test")]

        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]

        assert len(start_events) == 1
        assert len(tool_results) >= 1

    async def test_post_loop_drain(self) -> None:
        agent_queue: asyncio.Queue[Event] = asyncio.Queue()

        async def _stream_then_emit(
            messages: list[Message], tools: list[dict[str, Any]]
        ) -> AsyncGenerator[Event, None]:
            yield TextDelta(content="done")

        engine = QueryEngine(
            stream_fn=_stream_then_emit,
            tool_use_ctx=_make_ctx(),
            agent_event_queue=agent_queue,
        )

        async def _collect():
            events = []
            async for e in engine.submit_message("hi"):
                events.append(e)
                if isinstance(e, TextDelta):
                    await agent_queue.put(AgentStartEvent(agent_id="abc12345", task_id=1, prompt="late"))
            return events

        events = await _collect()
        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        assert len(start_events) == 1
        assert start_events[0].agent_id == "abc12345"

    async def test_mixed_completion_and_agent_events(self) -> None:
        completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        agent_queue: asyncio.Queue[Event] = asyncio.Queue()

        await completion_queue.put(
            AgentCompletionEvent(
                agent_id="abc12345",
                task_id=1,
                success=True,
                output="done",
                output_path="/tmp/out",
            )
        )
        await agent_queue.put(AgentStartEvent(agent_id="def67890", task_id=2, prompt="running"))

        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            completion_queue=completion_queue,
            agent_event_queue=agent_queue,
        )
        events = [e async for e in engine.submit_message("hi")]

        completions = [e for e in events if isinstance(e, AgentCompletionEvent)]
        starts = [e for e in events if isinstance(e, AgentStartEvent)]
        assert len(completions) == 1
        assert len(starts) == 1

    async def test_waiting_for_completion_preserves_agent_events(self) -> None:
        completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        agent_queue: asyncio.Queue[Event] = asyncio.Queue()
        active = {"count": 1}

        engine = QueryEngine(
            stream_fn=_stream_text_only,
            tool_use_ctx=_make_ctx(),
            completion_queue=completion_queue,
            agent_event_queue=agent_queue,
            active_agents_fn=lambda: active["count"],
        )

        async def _publish_later() -> None:
            await asyncio.sleep(0.05)
            await agent_queue.put(AgentStartEvent(agent_id="abc12345", task_id=1, prompt="waiting"))
            await completion_queue.put(
                AgentCompletionEvent(
                    agent_id="abc12345",
                    task_id=1,
                    success=True,
                    output="done",
                    output_path="/tmp/out",
                )
            )
            active["count"] = 0

        asyncio.create_task(_publish_later())
        events = [e async for e in engine.submit_message("hi")]

        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        completion_events = [e for e in events if isinstance(e, AgentCompletionEvent)]
        assert len(start_events) == 1
        assert len(completion_events) == 1

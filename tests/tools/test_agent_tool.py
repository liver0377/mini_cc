from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

from mini_cc.agent.models import AgentConfig, AgentStatus
from mini_cc.query_engine.state import (
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    QueryState,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.tools.agent_tool import AgentTool, AgentToolInput
from mini_cc.tools.base import ToolResult


def _make_agent_mock(
    agent_id: str = "a3f7b2c1",
    task_id: int = 1,
    events: list[Event] | None = None,
) -> MagicMock:
    agent = MagicMock()
    agent.config = AgentConfig(agent_id=agent_id, worktree_path="/tmp/wt")
    agent.task_id = task_id
    agent._status = AgentStatus.CREATED

    default_events: list[Event] = events or [TextDelta(content="result text")]

    async def _run(prompt: str) -> AsyncGenerator[Event, None]:
        agent._status = AgentStatus.RUNNING
        for e in default_events:
            yield e
        agent._status = AgentStatus.COMPLETED

    async def _run_background(prompt: str) -> None:
        agent._status = AgentStatus.BACKGROUND_RUNNING
        await asyncio.sleep(0)
        agent._status = AgentStatus.COMPLETED

    agent.run = _run
    agent.run_background = _run_background
    return agent


def _make_tool(
    default_timeout: int = 120,
) -> tuple[AgentTool, MagicMock, MagicMock]:
    manager = AsyncMock()
    state_fn = MagicMock(return_value=QueryState())
    tool = AgentTool(manager=manager, get_parent_state=state_fn, default_timeout=default_timeout)
    return tool, manager, state_fn


class TestAgentToolInput:
    def test_defaults(self):
        inp = AgentToolInput(prompt="do something")
        assert inp.prompt == "do something"
        assert inp.sync is True
        assert inp.fork is False

    def test_async_mode(self):
        inp = AgentToolInput(prompt="bg task", sync=False)
        assert inp.sync is False

    def test_fork_mode(self):
        inp = AgentToolInput(prompt="fork task", fork=True)
        assert inp.fork is True


class TestAgentToolProperties:
    def test_name(self):
        tool, _, _ = _make_tool()
        assert tool.name == "agent"

    def test_description_not_empty(self):
        tool, _, _ = _make_tool()
        assert len(tool.description) > 0

    def test_input_schema(self):
        tool, _, _ = _make_tool()
        assert tool.input_schema is AgentToolInput

    def test_execute_returns_fallback(self):
        tool, _, _ = _make_tool()
        result = tool.execute(prompt="test")
        assert "async_execute" in result.output


class TestSyncExecution:
    async def test_sync_returns_collected_output(self):
        tool, manager, _ = _make_tool()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="hello", sync=True)

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert "result text" in result.output
        assert agent._status == AgentStatus.COMPLETED

    async def test_sync_timeout_converts_to_background(self):
        tool, manager, _ = _make_tool(default_timeout=0)
        slow_events: list[Event] = [TextDelta(content="partial")]

        agent = _make_agent_mock(events=slow_events)

        async def _slow_run(prompt: str) -> AsyncGenerator[Event, None]:
            agent._status = AgentStatus.RUNNING
            await asyncio.sleep(2)
            for e in slow_events:
                yield e

        agent.run = _slow_run
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="slow task", sync=True)

        assert isinstance(result, ToolResult)
        assert "后台运行" in result.output
        assert agent._status == AgentStatus.BACKGROUND_RUNNING

    async def test_sync_fork_uses_parent_state(self):
        tool, manager, state_fn = _make_tool()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="fork task", sync=True, fork=True)

        assert isinstance(result, ToolResult)
        state_fn.assert_called_once()
        manager.create_agent.assert_called_once()
        call_kwargs = manager.create_agent.call_args
        assert call_kwargs.kwargs.get("fork") is True or call_kwargs[1].get("fork") is True


class TestAsyncExecution:
    async def test_async_returns_immediately(self):
        tool, manager, _ = _make_tool()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="bg task", sync=False)

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert agent.config.agent_id in result.output
        assert "已启动" in result.output

    async def test_async_does_not_block(self):
        tool, manager, _ = _make_tool()
        call_count = 0

        async def _slow_bg(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(10)

        agent = _make_agent_mock()
        agent.run_background = _slow_bg
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="bg", sync=False)

        assert isinstance(result, ToolResult)
        assert "已启动" in result.output


class TestAgentToolEventQueue:
    def _make_tool_with_queue(
        self, default_timeout: int = 120
    ) -> tuple[AgentTool, MagicMock, MagicMock, asyncio.Queue[Event]]:
        manager = AsyncMock()
        state_fn = MagicMock(return_value=QueryState())
        queue: asyncio.Queue[Event] = asyncio.Queue()
        tool = AgentTool(
            manager=manager,
            get_parent_state=state_fn,
            default_timeout=default_timeout,
            event_queue=queue,
        )
        return tool, manager, state_fn, queue

    async def test_sync_emits_agent_start_event(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="hello", sync=True)

        events: list[Event] = []
        while not queue.empty():
            events.append(await queue.get())
        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        assert len(start_events) == 1
        assert start_events[0].agent_id == "a3f7b2c1"
        assert start_events[0].prompt == "hello"

    async def test_sync_emits_tool_call_events(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        sub_events: list[Event] = [
            TextDelta(content="thinking"),
            ToolCallStart(tool_call_id="tc_1", name="file_read"),
            ToolResultEvent(tool_call_id="tc_1", name="file_read", output="file content", success=True),
            TextDelta(content="done"),
        ]
        agent = _make_agent_mock(events=sub_events)
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="read file", sync=True)

        events: list[Event] = []
        while not queue.empty():
            events.append(await queue.get())
        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        tc_events = [e for e in events if isinstance(e, AgentToolCallEvent)]
        tr_events = [e for e in events if isinstance(e, AgentToolResultEvent)]
        assert len(start_events) == 1
        assert len(tc_events) == 1
        assert tc_events[0].tool_name == "file_read"
        assert len(tr_events) == 1
        assert tr_events[0].success is True

    async def test_async_emits_start_event(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="bg task", sync=False)

        events: list[Event] = []
        while not queue.empty():
            events.append(await queue.get())
        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        assert len(start_events) == 1
        assert start_events[0].agent_id == "a3f7b2c1"

    async def test_no_queue_no_events_raised(self):
        manager = AsyncMock()
        state_fn = MagicMock(return_value=QueryState())
        tool = AgentTool(manager=manager, get_parent_state=state_fn)
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="hello", sync=True)
        assert result.success is True

    async def test_tool_result_preview_truncated(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        long_output = "x" * 200
        sub_events: list[Event] = [
            ToolResultEvent(tool_call_id="tc_1", name="bash", output=long_output, success=True),
        ]
        agent = _make_agent_mock(events=sub_events)
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="run cmd", sync=True)

        events: list[Event] = []
        while not queue.empty():
            events.append(await queue.get())
        tr_events = [e for e in events if isinstance(e, AgentToolResultEvent)]
        assert len(tr_events) == 1
        assert len(tr_events[0].output_preview) <= 103

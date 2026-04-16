from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

from mini_cc.harness import AgentBudget
from mini_cc.models import (
    AgentConfig,
    AgentStartEvent,
    AgentStatus,
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
    readonly: bool = False,
) -> MagicMock:
    agent = MagicMock()
    agent.config = AgentConfig(
        agent_id=agent_id,
        workspace_path="/tmp/project",
        is_readonly=readonly,
    )
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
        assert inp.readonly is False
        assert inp.fork is False
        assert inp.dispatch_plan_json is None

    def test_readonly_mode(self):
        inp = AgentToolInput(prompt="explore code", readonly=True)
        assert inp.readonly is True

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


class TestWriteExecution:
    async def test_write_returns_collected_output(self):
        tool, manager, _ = _make_tool()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="hello")

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert "result text" in result.output
        assert agent._status == AgentStatus.COMPLETED

    async def test_write_fork_uses_parent_state(self):
        tool, manager, state_fn = _make_tool()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="fork task", fork=True)

        assert isinstance(result, ToolResult)
        state_fn.assert_called_once()
        manager.create_agent.assert_called_once()
        call_kwargs = manager.create_agent.call_args
        assert call_kwargs.kwargs.get("fork") is True or call_kwargs[1].get("fork") is True

    async def test_write_default_is_not_readonly(self):
        tool, manager, _ = _make_tool()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="hello")

        call_kwargs = manager.create_agent.call_args
        assert call_kwargs.kwargs.get("readonly") is False


class TestReadonlyExecution:
    async def test_readonly_returns_immediately(self):
        tool, manager, _ = _make_tool()
        agent = _make_agent_mock(readonly=True)
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="bg task", readonly=True)

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert agent.config.agent_id in result.output
        assert "已启动" in result.output

    async def test_readonly_does_not_block(self):
        tool, manager, _ = _make_tool()
        call_count = 0

        async def _slow_bg(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(10)

        agent = _make_agent_mock(readonly=True)
        agent.run_background = _slow_bg
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="bg", readonly=True)

        assert isinstance(result, ToolResult)
        assert "已启动" in result.output

    async def test_readonly_creates_with_readonly_flag(self):
        tool, manager, _ = _make_tool()
        agent = _make_agent_mock(readonly=True)
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="explore", readonly=True)

        call_kwargs = manager.create_agent.call_args
        assert call_kwargs.kwargs.get("readonly") is True

    async def test_dispatch_plan_creates_multiple_readonly_agents(self):
        tool, manager, _ = _make_tool()
        manager.create_agent = AsyncMock(
            side_effect=[
                _make_agent_mock(agent_id="agent-1", task_id=1, readonly=True),
                _make_agent_mock(agent_id="agent-2", task_id=2, readonly=True),
            ]
        )
        dispatch_plan = json.dumps(
            {
                "goal": "分析项目",
                "root": "/tmp/project",
                "recommended_agent_count": 2,
                "dispatch_plan": [
                    {"index": 1, "scope": "src/agent", "mode": "readonly", "prompt": "analyze agent"},
                    {"index": 2, "scope": "src/tui", "mode": "readonly", "prompt": "analyze tui"},
                ],
                "overflow_scopes": [],
            }
        )

        result = await tool.async_execute(dispatch_plan_json=dispatch_plan)
        payload = json.loads(result.output)

        assert result.success is True
        assert payload["created_count"] == 2
        assert payload["agents"][0]["agent_id"] == "agent-1"
        assert manager.create_agent.await_count == 2

    async def test_dispatch_plan_rejects_non_readonly_mode(self):
        tool, manager, _ = _make_tool()
        dispatch_plan = json.dumps(
            {
                "goal": "修改项目",
                "root": "/tmp/project",
                "recommended_agent_count": 1,
                "dispatch_plan": [
                    {"index": 1, "scope": ".", "mode": "build", "prompt": "edit code"},
                ],
                "overflow_scopes": [],
            }
        )

        result = await tool.async_execute(dispatch_plan_json=dispatch_plan)

        assert result.success is False
        assert "仅支持 readonly agent" in result.output
        manager.create_agent.assert_not_called()

    async def test_empty_prompt_without_plan_fails(self):
        tool, manager, _ = _make_tool()

        result = await tool.async_execute(prompt="")

        assert result.success is False
        assert "prompt 不能为空" in result.output
        manager.create_agent.assert_not_called()

    async def test_write_budget_exhausted_blocks_new_write_agent(self):
        manager = AsyncMock()
        state_fn = MagicMock(return_value=QueryState())
        budget = AgentBudget(remaining_readonly=3, remaining_write=0)
        tool = AgentTool(
            manager=manager,
            get_parent_state=state_fn,
            get_budget=lambda: budget,
        )

        result = await tool.async_execute(prompt="edit code", readonly=False)

        assert result.success is False
        assert "写 Agent 预算已耗尽" in result.output
        manager.create_agent.assert_not_called()


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

    async def test_write_emits_agent_start_event(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="hello")

        events: list[Event] = []
        while not queue.empty():
            events.append(await queue.get())
        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        assert len(start_events) == 1
        assert start_events[0].agent_id == "a3f7b2c1"
        assert start_events[0].prompt == "hello"

    async def test_write_emits_tool_call_events(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        sub_events: list[Event] = [
            TextDelta(content="thinking"),
            ToolCallStart(tool_call_id="tc_1", name="file_read"),
            ToolResultEvent(tool_call_id="tc_1", name="file_read", output="file content", success=True),
            TextDelta(content="done"),
        ]
        agent = _make_agent_mock(events=sub_events)
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="read file")

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

    async def test_readonly_does_not_emit_start_from_tool(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        agent = _make_agent_mock(readonly=True)
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="bg task", readonly=True)

        events: list[Event] = []
        while not queue.empty():
            events.append(await queue.get())
        start_events = [e for e in events if isinstance(e, AgentStartEvent)]
        assert len(start_events) == 0

    async def test_no_queue_no_events_raised(self):
        manager = AsyncMock()
        state_fn = MagicMock(return_value=QueryState())
        tool = AgentTool(manager=manager, get_parent_state=state_fn)
        agent = _make_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="hello")
        assert result.success is True

    async def test_tool_result_preview_truncated(self):
        tool, manager, _, queue = self._make_tool_with_queue()
        long_output = "x" * 200
        sub_events: list[Event] = [
            ToolResultEvent(tool_call_id="tc_1", name="bash", output=long_output, success=True),
        ]
        agent = _make_agent_mock(events=sub_events)
        manager.create_agent = AsyncMock(return_value=agent)

        await tool.async_execute(prompt="run cmd")

        events: list[Event] = []
        while not queue.empty():
            events.append(await queue.get())
        tr_events = [e for e in events if isinstance(e, AgentToolResultEvent)]
        assert len(tr_events) == 1
        assert len(tr_events[0].output_preview) <= 103

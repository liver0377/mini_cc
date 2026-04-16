from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

from mini_cc.agent.manager import AgentManager
from mini_cc.context.system_prompt import EnvInfo, SystemPromptBuilder
from mini_cc.models import AgentCompletionEvent, AgentConfig, Event, QueryState, Role, TextDelta
from mini_cc.task.service import TaskService
from mini_cc.tools.agent_tool import AgentTool


async def _noop_stream(messages, schemas):
    yield TextDelta(content="done")


def _make_env_info() -> EnvInfo:
    return EnvInfo(
        working_directory="/tmp/test",
        is_git_repo=False,
        platform_name="linux",
        shell="/bin/bash",
        os_version="Linux 6.0",
        model_name="Test Model",
        model_id="test-model",
    )


def _make_tool_agent_mock(
    agent_id: str = "a3f7b2c1",
    task_id: int = 1,
    events: list[Event] | None = None,
) -> MagicMock:
    agent = MagicMock()
    agent.config = AgentConfig(agent_id=agent_id, workspace_path="/tmp/project")
    agent.task_id = task_id
    default_events: list[Event] = events or [TextDelta(content="done")]

    async def _run(prompt: str) -> AsyncGenerator[Event, None]:
        for e in default_events:
            yield e

    async def _run_background(prompt: str) -> None:
        pass

    agent.run = _run
    agent.run_background = _run_background
    return agent


class TestModePropagation:
    async def test_create_agent_with_plan_mode(self, tmp_path):
        task_service = TaskService(task_list_id="test-mode-plan", base_dir=tmp_path / "tasks")
        completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        prompt_builder = SystemPromptBuilder()
        env_info = _make_env_info()

        manager = AgentManager(
            project_root=tmp_path / "project",
            stream_fn=_noop_stream,
            task_service=task_service,
            completion_queue=completion_queue,
            prompt_builder=prompt_builder,
            env_info=env_info,
        )

        agent = await manager.create_agent(prompt="test plan mode", mode="plan")

        assert len(agent.state.messages) >= 1
        assert agent.state.messages[0].role == Role.SYSTEM
        content = agent.state.messages[0].content or ""
        assert "plan" in content.lower()
        assert "子 Agent" in content

    async def test_create_agent_with_build_mode(self, tmp_path):
        task_service = TaskService(task_list_id="test-mode-build", base_dir=tmp_path / "tasks")
        completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        prompt_builder = SystemPromptBuilder()
        env_info = _make_env_info()

        manager = AgentManager(
            project_root=tmp_path / "project",
            stream_fn=_noop_stream,
            task_service=task_service,
            completion_queue=completion_queue,
            prompt_builder=prompt_builder,
            env_info=env_info,
        )

        agent = await manager.create_agent(prompt="test build mode", mode="build")

        assert len(agent.state.messages) >= 1
        assert agent.state.messages[0].role == Role.SYSTEM
        content = agent.state.messages[0].content or ""
        assert "build" in content.lower()
        assert "子 Agent" in content

    async def test_create_agent_without_prompt_builder_no_system_message(self, tmp_path):
        task_service = TaskService(task_list_id="test-no-pb", base_dir=tmp_path / "tasks")
        completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()

        manager = AgentManager(
            project_root=tmp_path / "project",
            stream_fn=_noop_stream,
            task_service=task_service,
            completion_queue=completion_queue,
        )

        agent = await manager.create_agent(prompt="no pb")

        assert len(agent.state.messages) == 1
        assert agent.state.messages[0].role == Role.SYSTEM
        assert "子 Agent" in (agent.state.messages[0].content or "")

    async def test_agent_tool_passes_mode(self):
        manager = AsyncMock()
        state_fn = MagicMock(return_value=QueryState())
        mode_fn = MagicMock(return_value="plan")
        tool = AgentTool(
            manager=manager,
            get_parent_state=state_fn,
            get_mode=mode_fn,
        )

        agent = _make_tool_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="test mode pass")
        assert result.success is True

        call_kwargs = manager.create_agent.call_args
        assert call_kwargs.kwargs.get("mode") == "plan"

    async def test_agent_tool_default_mode_is_build(self):
        manager = AsyncMock()
        state_fn = MagicMock(return_value=QueryState())
        tool = AgentTool(
            manager=manager,
            get_parent_state=state_fn,
        )

        agent = _make_tool_agent_mock()
        manager.create_agent = AsyncMock(return_value=agent)

        result = await tool.async_execute(prompt="test default mode")
        assert result.success is True

        call_kwargs = manager.create_agent.call_args
        assert call_kwargs.kwargs.get("mode") == "build"

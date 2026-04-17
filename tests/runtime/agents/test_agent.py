from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mini_cc.models import (
    AgentCompletionEvent,
    AgentConfig,
    AgentId,
    AgentStartEvent,
    AgentStatus,
    AgentTextDeltaEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    Role,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    generate_agent_id,
)
from mini_cc.runtime.agents import AgentManager, build_workspace_notice
from mini_cc.task.service import TaskService


@pytest.fixture
def task_service(tmp_path):
    return TaskService(task_list_id="test-agent-session", base_dir=tmp_path / "tasks")


@pytest.fixture
def completion_queue():
    return asyncio.Queue()


async def _noop_stream(messages, schemas):
    yield TextDelta(content="done")


def _make_manager(
    tmp_path,
    task_service,
    completion_queue,
    stream_fn=None,
):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    return AgentManager(
        project_root=project_root,
        stream_fn=stream_fn or _noop_stream,
        task_service=task_service,
        completion_queue=completion_queue,
    )


class TestAgentId:
    def test_generate_is_8_hex_chars(self):
        aid = generate_agent_id()
        assert len(aid) == 8
        int(aid, 16)

    def test_generate_unique(self):
        ids = {generate_agent_id() for _ in range(100)}
        assert len(ids) == 100

    def test_agent_id_eq(self):
        a = AgentId(value="abc12345")
        b = AgentId(value="abc12345")
        assert a == b

    def test_agent_id_hash(self):
        a = AgentId(value="abc12345")
        b = AgentId(value="abc12345")
        assert hash(a) == hash(b)

    def test_agent_id_str(self):
        aid = AgentId(value="a3f7b2c1")
        assert str(aid) == "a3f7b2c1"


class TestAgentConfig:
    def test_basic_config(self):
        cfg = AgentConfig(agent_id="a3f7b2c1", workspace_path="/tmp/project")
        assert cfg.agent_id == "a3f7b2c1"
        assert cfg.is_fork is False
        assert cfg.is_readonly is False
        assert cfg.parent_agent_id is None
        assert cfg.timeout_seconds == 120
        assert cfg.scope_paths == []

    def test_readonly_config(self):
        cfg = AgentConfig(
            agent_id="c1d2e3f4",
            workspace_path="/tmp/project",
            is_readonly=True,
            scope_paths=["src"],
            base_version_stamp="abc:clean:1",
        )
        assert cfg.is_readonly is True
        assert cfg.scope_paths == ["src"]
        assert cfg.base_version_stamp == "abc:clean:1"


class TestWorkspaceNotice:
    def test_contains_project_root(self):
        cfg = AgentConfig(agent_id="a3f7b2c1", workspace_path="/project")
        notice = build_workspace_notice(cfg, Path("/project"))
        assert "/project" in notice
        assert "无需路径翻译" in notice


class TestSubAgent:
    async def test_run_completes(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="test task", readonly=False, scope_paths=["src"])

        events: list[Event] = []
        async for event in agent.run("test task"):
            events.append(event)

        assert agent.status == AgentStatus.COMPLETED
        assert any(isinstance(e, TextDelta) for e in events)

        completion: AgentCompletionEvent = await asyncio.wait_for(completion_queue.get(), timeout=1.0)
        assert completion.agent_id == agent.config.agent_id
        assert completion.success is True

    async def test_run_background_completes(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="bg task", readonly=True)

        await agent.run_background("bg task")

        completion: AgentCompletionEvent = await asyncio.wait_for(completion_queue.get(), timeout=1.0)
        assert agent.status == AgentStatus.COMPLETED
        assert completion.success is True

    async def test_run_background_emits_events(self, tmp_path, task_service, completion_queue):
        event_queue: asyncio.Queue[Event] = asyncio.Queue()

        async def _stream_with_tools(messages, schemas):
            if any(m.role == Role.TOOL for m in messages):
                yield TextDelta(content="done")
                return
            yield TextDelta(content="reading")
            yield ToolCallStart(tool_call_id="tc_1", name="file_read")
            yield ToolCallDelta(tool_call_id="tc_1", arguments_json_delta='{"file_path":"/tmp/a"}')
            yield ToolCallEnd(tool_call_id="tc_1")

        manager = _make_manager(tmp_path, task_service, completion_queue, stream_fn=_stream_with_tools)
        agent = await manager.create_agent(prompt="bg tool test", readonly=True)
        agent._event_queue = event_queue

        await agent.run_background("bg tool test")

        events: list[Event] = []
        while not event_queue.empty():
            events.append(await event_queue.get())
        assert any(isinstance(e, AgentStartEvent) for e in events)
        assert any(isinstance(e, AgentToolCallEvent) for e in events)
        assert any(isinstance(e, AgentToolResultEvent) for e in events)
        assert any(isinstance(e, AgentTextDeltaEvent) for e in events)

    async def test_readonly_completion_reports_stale_if_workspace_changed(
        self,
        tmp_path,
        task_service,
        completion_queue,
    ):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        stamps = iter(["base-1", "completed-2"])
        manager._workspace_version_stamp = MagicMock(side_effect=lambda: next(stamps))
        agent = await manager.create_agent(prompt="inspect", readonly=True)

        await agent.run_background("inspect")

        completion: AgentCompletionEvent = await asyncio.wait_for(completion_queue.get(), timeout=1.0)
        assert completion.base_version_stamp == "base-1"
        assert completion.completed_version_stamp == "completed-2"
        assert completion.is_stale is True


class TestAgentManager:
    async def test_create_readonly_agent_uses_workspace(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="explore code", readonly=True)

        assert agent.config.is_readonly is True
        assert agent.config.workspace_path == str(tmp_path / "project")
        assert agent.snapshot_svc is None

    async def test_create_write_agent_uses_snapshot_and_scope(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="fix bug", readonly=False, scope_paths=["src/core"])

        assert agent.config.is_readonly is False
        assert agent.config.workspace_path == str(tmp_path / "project")
        assert agent.snapshot_svc is not None
        assert agent.config.scope_paths == ["src/core"]

    async def test_write_scope_conflict_rejected(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        await manager.create_agent(prompt="first writer", readonly=False, scope_paths=["src"])

        with pytest.raises(ValueError, match="scope 冲突"):
            await manager.create_agent(prompt="second writer", readonly=False, scope_paths=["src/api"])

    async def test_non_overlapping_write_scopes_allowed(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        await manager.create_agent(prompt="writer one", readonly=False, scope_paths=["src"])
        agent = manager.get_agent(next(iter(manager.agents)))
        assert agent is not None
        agent._status = AgentStatus.COMPLETED

        second = await manager.create_agent(prompt="writer two", readonly=False, scope_paths=["tests"])
        assert second.config.scope_paths == ["tests"]

    async def test_cleanup_removes_output_and_snapshot(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="cleanup test", readonly=False, scope_paths=["src"])
        agent_id = agent.config.agent_id

        output_dir = tmp_path / "project" / ".mini_cc" / "tasks"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{agent_id}.output").write_text("result", encoding="utf-8")
        snapshot_dir = tmp_path / "project" / ".mini_cc" / "snapshots" / agent_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "_manifest.json").write_text('{"agent_id": "x", "files": {}}', encoding="utf-8")

        await manager.cleanup(agent_id)

        assert manager.get_agent(agent_id) is None
        assert not (output_dir / f"{agent_id}.output").exists()
        assert not snapshot_dir.exists()

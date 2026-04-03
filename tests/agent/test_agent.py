from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mini_cc.agent.manager import AgentManager
from mini_cc.agent.models import AgentConfig, AgentId, AgentStatus, generate_agent_id
from mini_cc.agent.sub_agent import build_worktree_notice
from mini_cc.agent.worktree import WorktreeService
from mini_cc.query_engine.state import Event, QueryState, TextDelta
from mini_cc.task.models import AgentCompletionEvent
from mini_cc.task.service import TaskService


@pytest.fixture
def task_service(tmp_path):
    return TaskService(task_list_id="test-agent-session", base_dir=tmp_path / "tasks")


@pytest.fixture
def completion_queue():
    return asyncio.Queue()


async def _noop_stream(messages, schemas):
    yield TextDelta(content="done")


def _make_worktree_svc(tmp_path):
    svc = MagicMock(spec=WorktreeService)
    wt_base = tmp_path / "project" / ".mini_cc" / "worktrees"
    out_base = tmp_path / "project" / ".mini_cc" / "tasks"

    def _create(agent_id, ref="HEAD"):
        wt = wt_base / agent_id
        wt.mkdir(parents=True, exist_ok=True)
        return wt

    svc.create = _create
    svc.remove = MagicMock()
    svc.cleanup_output = MagicMock()
    svc.output_dir = out_base
    return svc


def _make_manager(
    tmp_path,
    task_service,
    completion_queue,
    stream_fn=None,
):
    return AgentManager(
        project_root=tmp_path / "project",
        stream_fn=stream_fn or _noop_stream,
        task_service=task_service,
        completion_queue=completion_queue,
        worktree_service=_make_worktree_svc(tmp_path),
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
        cfg = AgentConfig(
            agent_id="a3f7b2c1",
            worktree_path="/tmp/worktree",
        )
        assert cfg.agent_id == "a3f7b2c1"
        assert cfg.is_fork is False
        assert cfg.parent_agent_id is None
        assert cfg.timeout_seconds == 120

    def test_fork_config(self):
        cfg = AgentConfig(
            agent_id="f8e4d9c0",
            worktree_path="/tmp/worktree2",
            is_fork=True,
            parent_agent_id="a3f7b2c1",
        )
        assert cfg.is_fork is True
        assert cfg.parent_agent_id == "a3f7b2c1"

    def test_worktree_property(self):
        from pathlib import Path

        cfg = AgentConfig(agent_id="abc", worktree_path="/tmp/wt")
        assert cfg.worktree == Path("/tmp/wt")

    def test_serialization(self):
        cfg = AgentConfig(agent_id="abc", worktree_path="/tmp/wt", is_fork=True, parent_agent_id="p")
        data = cfg.model_dump()
        restored = AgentConfig.model_validate(data)
        assert restored == cfg


class TestAgentStatus:
    def test_values(self):
        assert AgentStatus.CREATED == "created"
        assert AgentStatus.RUNNING == "running"
        assert AgentStatus.BACKGROUND_RUNNING == "background_running"
        assert AgentStatus.COMPLETED == "completed"
        assert AgentStatus.CANCELLED == "cancelled"


class TestBuildWorktreeNotice:
    def test_contains_paths(self):
        cfg = AgentConfig(agent_id="a3f7b2c1", worktree_path="/project/.mini_cc/worktrees/a3f7b2c1")
        notice = build_worktree_notice(cfg, Path("/project"))
        assert "/project" in notice
        assert str(cfg.worktree_path) in notice


class TestSubAgent:
    async def test_run_completes(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="test task", sync=True)

        assert agent.status == AgentStatus.CREATED

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
        agent = await manager.create_agent(prompt="bg task", sync=False)

        await agent.run_background("bg task")

        assert agent.status == AgentStatus.COMPLETED

        completion: AgentCompletionEvent = await asyncio.wait_for(completion_queue.get(), timeout=1.0)
        assert completion.success is True

    async def test_cancel_sets_flag(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="cancel test")

        agent.cancel()
        assert agent._cancel_event.is_set()

    async def test_output_file_written(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="output test")

        async for _ in agent.run("output test"):
            pass

        completion: AgentCompletionEvent = await asyncio.wait_for(completion_queue.get(), timeout=1.0)
        assert completion.output_path.exists()
        content = completion.output_path.read_text(encoding="utf-8")
        assert agent.config.agent_id in content


class TestAgentManager:
    async def test_create_agent(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="hello")

        assert agent.config.agent_id in manager.agents
        assert agent.config.is_fork is False
        assert agent.status == AgentStatus.CREATED

    async def test_create_fork_agent(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        parent_state = QueryState()
        agent = await manager.create_agent(prompt="fork task", fork=True, parent_state=parent_state)

        assert agent.config.is_fork is True
        assert len(agent.state.messages) > 0

    async def test_get_agent(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="find me")

        found = manager.get_agent(agent.config.agent_id)
        assert found is agent

    async def test_get_nonexistent(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        assert manager.get_agent("nonexistent") is None

    async def test_cleanup(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        agent = await manager.create_agent(prompt="cleanup test")
        agent_id = agent.config.agent_id

        await manager.cleanup(agent_id)

        assert manager.get_agent(agent_id) is None
        manager._worktree_svc.remove.assert_called_once_with(agent_id)
        manager._worktree_svc.cleanup_output.assert_called_once_with(agent_id)

    async def test_cleanup_nonexistent(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)
        await manager.cleanup("nonexistent")

    async def test_multiple_agents(self, tmp_path, task_service, completion_queue):
        manager = _make_manager(tmp_path, task_service, completion_queue)

        await manager.create_agent(prompt="task 1")
        await manager.create_agent(prompt="task 2")
        await manager.create_agent(prompt="task 3")

        assert len(manager.agents) == 3
        ids = {a.config.agent_id for a in manager.agents.values()}
        assert len(ids) == 3

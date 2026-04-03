from __future__ import annotations

import pytest

from mini_cc.task.models import Task, TaskStatus, TaskType
from mini_cc.task.service import TaskService


@pytest.fixture
def service(tmp_path) -> TaskService:
    return TaskService(task_list_id="test-session", base_dir=tmp_path)


class TestTaskModels:
    def test_local_agent_task(self):
        task = Task(
            id=1,
            type=TaskType.LOCAL_AGENT,
            subject="Fix auth bug",
            description="Fix the authentication bypass vulnerability",
            active_form="Fixing auth bug",
            owner="agent-a3f7b2c1",
            status=TaskStatus.IN_PROGRESS,
            agent_id="a3f7b2c1",
            prompt="Fix the authentication bypass vulnerability",
        )
        assert task.type == TaskType.LOCAL_AGENT
        assert task.agent_id == "a3f7b2c1"
        assert task.is_fork is False

    def test_local_bash_task(self):
        task = Task(
            id=2,
            type=TaskType.LOCAL_BASH,
            subject="Run test suite",
            description="Run the full test suite",
            command="pytest tests/ -v",
            status=TaskStatus.PENDING,
        )
        assert task.type == TaskType.LOCAL_BASH
        assert task.command == "pytest tests/ -v"

    def test_fork_agent_task(self):
        task = Task(
            id=3,
            type=TaskType.LOCAL_AGENT,
            subject="Summarize context",
            description="Summarize the current conversation context",
            agent_id="f8e4d9c0",
            is_fork=True,
            parent_agent_id="main",
            prompt="Summarize the current conversation context",
        )
        assert task.is_fork is True
        assert task.parent_agent_id == "main"

    def test_task_with_dependencies(self):
        task = Task(
            id=4,
            type=TaskType.LOCAL_AGENT,
            subject="Deploy to staging",
            description="Deploy the changes to staging",
            blocked_by=[1, 2],
        )
        assert task.blocked_by == [1, 2]
        assert task.blocks == []

    def test_task_serialization_roundtrip(self):
        task = Task(
            id=5,
            type=TaskType.LOCAL_AGENT,
            subject="Write docs",
            description="Write API documentation",
            metadata={"priority": "high"},
        )
        json_str = task.model_dump_json()
        restored = Task.model_validate_json(json_str)
        assert restored == task


class TestTaskService:
    async def test_create_task(self, service):
        task = await service.create(
            type=TaskType.LOCAL_AGENT,
            subject="Test task",
            description="A test task",
        )
        assert task.id == 1
        assert task.status == TaskStatus.PENDING
        assert task.type == TaskType.LOCAL_AGENT

    async def test_auto_increment_ids(self, service):
        t1 = await service.create(type=TaskType.LOCAL_AGENT, subject="T1", description="d1")
        t2 = await service.create(type=TaskType.LOCAL_AGENT, subject="T2", description="d2")
        assert t1.id == 1
        assert t2.id == 2

    async def test_get_task(self, service):
        created = await service.create(type=TaskType.LOCAL_AGENT, subject="T", description="d")
        fetched = await service.get(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.subject == "T"

    async def test_get_nonexistent(self, service):
        assert await service.get(999) is None

    async def test_list_all(self, service):
        await service.create(type=TaskType.LOCAL_AGENT, subject="T1", description="d1")
        await service.create(type=TaskType.LOCAL_AGENT, subject="T2", description="d2")
        tasks = await service.list_all()
        assert len(tasks) == 2

    async def test_update_task(self, service):
        created = await service.create(type=TaskType.LOCAL_AGENT, subject="T", description="d")
        updated = await service.update(created.id, subject="Updated", status=TaskStatus.IN_PROGRESS)
        assert updated.subject == "Updated"
        assert updated.status == TaskStatus.IN_PROGRESS

    async def test_claim_task(self, service):
        created = await service.create(type=TaskType.LOCAL_AGENT, subject="T", description="d")
        claimed = await service.claim(created.id, owner="agent-abc")
        assert claimed.owner == "agent-abc"
        assert claimed.status == TaskStatus.IN_PROGRESS

    async def test_complete_task(self, service):
        created = await service.create(type=TaskType.LOCAL_AGENT, subject="T", description="d")
        completed = await service.complete(created.id)
        assert completed.status == TaskStatus.COMPLETED

    async def test_fail_task(self, service):
        created = await service.create(type=TaskType.LOCAL_AGENT, subject="T", description="d")
        failed = await service.fail(created.id, error="OOM")
        assert failed.status == TaskStatus.FAILED
        assert failed.metadata.get("error") == "OOM"

    async def test_cancel_task(self, service):
        created = await service.create(type=TaskType.LOCAL_AGENT, subject="T", description="d")
        await service.cancel(created.id)
        fetched = await service.get(created.id)
        assert fetched is not None
        assert fetched.status == TaskStatus.CANCELLED

    async def test_complete_unblocks_downstream(self, service):
        t1 = await service.create(type=TaskType.LOCAL_AGENT, subject="T1", description="d1")
        t2 = await service.create(
            type=TaskType.LOCAL_AGENT,
            subject="T2",
            description="d2",
            blocked_by=[t1.id],
        )

        assert t2.blocked_by == [t1.id]

        await service.complete(t1.id)

        t2_refreshed = await service.get(t2.id)
        assert t2_refreshed is not None
        assert t2_refreshed.blocked_by == []

    async def test_cancel_removes_references(self, service):
        t1 = await service.create(
            type=TaskType.LOCAL_AGENT,
            subject="T1",
            description="d1",
            blocks=[2],
        )
        t2 = await service.create(
            type=TaskType.LOCAL_AGENT,
            subject="T2",
            description="d2",
            blocked_by=[t1.id],
        )

        await service.cancel(t1.id)

        t2_refreshed = await service.get(t2.id)
        assert t2_refreshed is not None
        assert t2_refreshed.blocked_by == []

    async def test_get_ready_tasks(self, service):
        t1 = await service.create(type=TaskType.LOCAL_AGENT, subject="T1", description="d1")
        t2 = await service.create(
            type=TaskType.LOCAL_AGENT,
            subject="T2",
            description="d2",
            blocked_by=[t1.id],
        )
        t3 = await service.create(type=TaskType.LOCAL_AGENT, subject="T3", description="d3")

        ready = await service.get_ready_tasks()
        ready_ids = [t.id for t in ready]
        assert t1.id in ready_ids
        assert t2.id not in ready_ids
        assert t3.id in ready_ids

        await service.complete(t1.id)

        ready = await service.get_ready_tasks()
        ready_ids = [t.id for t in ready]
        assert t2.id in ready_ids

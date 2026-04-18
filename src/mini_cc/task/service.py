from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mini_cc.models import Task, TaskStatus

_DEFAULT_BASE_DIR = Path.home() / ".local" / "share" / "mini_cc" / "tasks"
_LOCK_RETRY_ATTEMPTS = 5
_LOCK_RETRY_BASE_DELAY = 0.05

_VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


class TaskService:
    def __init__(self, task_list_id: str, base_dir: Path | None = None) -> None:
        self._task_dir = (base_dir or _DEFAULT_BASE_DIR) / task_list_id
        self._task_dir.mkdir(parents=True, exist_ok=True)
        self._id_lock = asyncio.Lock()
        self._next_id = self._compute_next_id()

    def _compute_next_id(self) -> int:
        max_id = 0
        for p in self._task_dir.glob("*.json"):
            try:
                max_id = max(max_id, int(p.stem))
            except ValueError:
                continue
        return max_id + 1

    def _task_path(self, task_id: int) -> Path:
        return self._task_dir / f"{task_id}.json"

    def _read_task(self, path: Path) -> Task:
        return Task.model_validate_json(path.read_text(encoding="utf-8"))

    def _write_task(self, path: Path, task: Task) -> None:
        path.write_text(task.model_dump_json(indent=2), encoding="utf-8")

    async def _with_lock(self, path: Path, fn: Callable[[Any], Task]) -> Task:
        import fcntl

        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                fd = path.open("r+")
            except FileNotFoundError:
                fd = path.open("w+")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                task = fn(fd)
                return task
            except BlockingIOError:
                fd.close()
                delay = _LOCK_RETRY_BASE_DELAY * (2**attempt)
                await asyncio.sleep(delay)
                continue
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
        raise TimeoutError(f"Failed to acquire lock for {path}")

    def _validate_transition(self, current: TaskStatus, target: TaskStatus) -> None:
        allowed = _VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise ValueError(f"Invalid task state transition: {current} -> {target}")

    async def create(self, task: Task | None = None, **kwargs: Any) -> Task:
        async with self._id_lock:
            if task is None:
                task = Task(**kwargs)
            task.id = self._next_id
            self._next_id += 1
            self._check_cycle(task)
            path = self._task_path(task.id)
            self._write_task(path, task)
            return task

    async def get(self, task_id: int) -> Task | None:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        return self._read_task(path)

    async def list_all(self) -> list[Task]:
        tasks: list[Task] = []
        for p in sorted(self._task_dir.glob("*.json"), key=lambda p: int(p.stem)):
            tasks.append(self._read_task(p))
        return tasks

    async def update(self, task_id: int, **kwargs: Any) -> Task:
        path = self._task_path(task_id)

        def _do(fd: Any) -> Task:
            fd.seek(0)
            data = json.load(fd)
            data.update(kwargs)
            task = Task.model_validate(data)
            fd.seek(0)
            fd.truncate()
            fd.write(task.model_dump_json(indent=2))
            return task

        return await self._with_lock(path, _do)

    async def claim(self, task_id: int, owner: str) -> Task:
        existing = await self.get(task_id)
        if existing is not None:
            self._validate_transition(existing.status, TaskStatus.IN_PROGRESS)
        return await self.update(task_id, owner=owner, status=TaskStatus.IN_PROGRESS)

    async def complete(self, task_id: int) -> Task:
        existing = await self.get(task_id)
        if existing is not None:
            self._validate_transition(existing.status, TaskStatus.COMPLETED)

        path = self._task_path(task_id)

        def _do(fd: Any) -> Task:
            fd.seek(0)
            data = json.load(fd)
            data["status"] = TaskStatus.COMPLETED
            data["revision"] = data.get("revision", 0) + 1
            task = Task.model_validate(data)
            fd.seek(0)
            fd.truncate()
            fd.write(task.model_dump_json(indent=2))
            return task

        task = await self._with_lock(path, _do)
        await self._unblock_downstream(task_id)
        return task

    async def fail(self, task_id: int, error: str) -> Task:
        existing = await self.get(task_id)
        if existing is not None:
            self._validate_transition(existing.status, TaskStatus.FAILED)
        return await self.update(task_id, status=TaskStatus.FAILED, metadata={"error": error})

    async def cancel(self, task_id: int) -> None:
        existing = await self.get(task_id)
        if existing is not None:
            self._validate_transition(existing.status, TaskStatus.CANCELLED)

        path = self._task_path(task_id)

        def _do(fd: Any) -> Task:
            fd.seek(0)
            data = json.load(fd)
            data["status"] = TaskStatus.CANCELLED
            data["revision"] = data.get("revision", 0) + 1
            task = Task.model_validate(data)
            fd.seek(0)
            fd.truncate()
            fd.write(task.model_dump_json(indent=2))
            return task

        await self._with_lock(path, _do)
        await self._remove_references(task_id)

    async def get_ready_tasks(self) -> list[Task]:
        tasks = await self.list_all()
        completed_ids = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
        return [t for t in tasks if t.status == TaskStatus.PENDING and all(b in completed_ids for b in t.blocked_by)]

    async def _unblock_downstream(self, completed_id: int) -> None:
        tasks = await self.list_all()
        for task in tasks:
            if completed_id in task.blocked_by:
                task.blocked_by.remove(completed_id)
                self._write_task(self._task_path(task.id), task)

    async def _remove_references(self, removed_id: int) -> None:
        tasks = await self.list_all()
        for task in tasks:
            changed = False
            if removed_id in task.blocks:
                task.blocks.remove(removed_id)
                changed = True
            if removed_id in task.blocked_by:
                task.blocked_by.remove(removed_id)
                changed = True
            if changed:
                self._write_task(self._task_path(task.id), task)

    def _check_cycle(self, task: Task) -> None:
        if not task.blocked_by:
            return
        existing: dict[int, list[int]] = {}
        for p in self._task_dir.glob("*.json"):
            try:
                t = self._read_task(p)
                existing[t.id] = t.blocks
            except (ValueError, OSError):
                continue
        existing[task.id] = task.blocks

        adj: dict[int, list[int]] = {}
        for tid, blocks_list in existing.items():
            for blocked_id in blocks_list:
                adj.setdefault(blocked_id, []).append(tid)

        visited: set[int] = set()
        on_stack: set[int] = set()

        def _has_cycle(node: int) -> bool:
            visited.add(node)
            on_stack.add(node)
            for neighbor in adj.get(node, []):
                if neighbor not in visited:
                    if _has_cycle(neighbor):
                        return True
                elif neighbor in on_stack:
                    return True
            on_stack.discard(node)
            return False

        for node in existing:
            if node not in visited:
                if _has_cycle(node):
                    raise ValueError("检测到循环依赖: 任务依赖关系形成环路")

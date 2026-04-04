from __future__ import annotations

import fcntl
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mini_cc.task.models import Task, TaskStatus

_DEFAULT_BASE_DIR = Path.home() / ".local" / "share" / "mini_cc" / "tasks"
_LOCK_RETRY_ATTEMPTS = 5
_LOCK_RETRY_BASE_DELAY = 0.05


class TaskService:
    def __init__(self, task_list_id: str, base_dir: Path | None = None) -> None:
        self._task_dir = (base_dir or _DEFAULT_BASE_DIR) / task_list_id
        self._task_dir.mkdir(parents=True, exist_ok=True)
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

    def _with_lock(self, path: Path, fn: Callable[[Any], Task]) -> Task:
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
                time.sleep(delay)
                continue
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
        raise TimeoutError(f"Failed to acquire lock for {path}")

    async def create(self, task: Task | None = None, **kwargs: Any) -> Task:
        if task is None:
            task = Task(**kwargs)
        task.id = self._next_id
        self._next_id += 1
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

        return self._with_lock(path, _do)

    async def claim(self, task_id: int, owner: str) -> Task:
        return await self.update(task_id, owner=owner, status=TaskStatus.IN_PROGRESS)

    async def complete(self, task_id: int) -> Task:
        path = self._task_path(task_id)

        def _do(fd: Any) -> Task:
            fd.seek(0)
            data = json.load(fd)
            data["status"] = TaskStatus.COMPLETED
            task = Task.model_validate(data)
            fd.seek(0)
            fd.truncate()
            fd.write(task.model_dump_json(indent=2))
            return task

        task = self._with_lock(path, _do)
        await self._unblock_downstream(task_id)
        return task

    async def fail(self, task_id: int, error: str) -> Task:
        return await self.update(task_id, status=TaskStatus.FAILED, metadata={"error": error})

    async def cancel(self, task_id: int) -> None:
        path = self._task_path(task_id)

        def _do(fd: Any) -> Task:
            fd.seek(0)
            data = json.load(fd)
            data["status"] = TaskStatus.CANCELLED
            task = Task.model_validate(data)
            fd.seek(0)
            fd.truncate()
            fd.write(task.model_dump_json(indent=2))
            return task

        self._with_lock(path, _do)
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

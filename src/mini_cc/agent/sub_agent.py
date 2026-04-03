from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from mini_cc.agent.models import AgentConfig, AgentStatus
from mini_cc.query_engine.engine import QueryEngine
from mini_cc.query_engine.state import (
    Event,
    QueryState,
    TextDelta,
)
from mini_cc.task.models import AgentCompletionEvent
from mini_cc.task.service import TaskService


class SubAgent:
    def __init__(
        self,
        config: AgentConfig,
        engine: QueryEngine,
        state: QueryState,
        task_id: int,
        task_service: TaskService,
        completion_queue: asyncio.Queue[AgentCompletionEvent],
        output_dir: Path,
    ) -> None:
        self._config = config
        self._engine = engine
        self._state = state
        self._task_id = task_id
        self._task_service = task_service
        self._completion_queue = completion_queue
        self._output_dir = output_dir
        self._status = AgentStatus.CREATED
        self._cancel_event = asyncio.Event()
        self._collected_output: list[str] = []

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def status(self) -> AgentStatus:
        return self._status

    @property
    def task_id(self) -> int:
        return self._task_id

    @property
    def state(self) -> QueryState:
        return self._state

    async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
        self._status = AgentStatus.RUNNING
        await self._task_service.claim(self._task_id, owner=f"agent-{self._config.agent_id}")

        try:
            async for event in self._engine.submit_message(prompt, self._state):
                if self._cancel_event.is_set():
                    self._status = AgentStatus.CANCELLED
                    await self._task_service.cancel(self._task_id)
                    return
                self._collect_text(event)
                yield event

            await self._finish(success=True)
        except Exception as e:
            await self._finish(success=False, error=str(e))
            raise

    async def run_background(self, prompt: str) -> None:
        self._status = AgentStatus.BACKGROUND_RUNNING
        await self._task_service.claim(self._task_id, owner=f"agent-{self._config.agent_id}")

        try:
            async for event in self._engine.submit_message(prompt, self._state):
                if self._cancel_event.is_set():
                    self._status = AgentStatus.CANCELLED
                    await self._task_service.cancel(self._task_id)
                    return
                self._collect_text(event)

            await self._finish(success=True)
        except Exception as e:
            await self._finish(success=False, error=str(e))

    def cancel(self) -> None:
        self._cancel_event.set()

    def _collect_text(self, event: Event) -> None:
        if isinstance(event, TextDelta):
            self._collected_output.append(event.content)

    async def _finish(self, *, success: bool, error: str | None = None) -> None:
        output_text = "".join(self._collected_output)
        truncated = output_text[:500]

        if success:
            self._status = AgentStatus.COMPLETED
            await self._task_service.complete(self._task_id)
        else:
            self._status = AgentStatus.CANCELLED
            if error:
                await self._task_service.fail(self._task_id, error=error)

        output_path = self._write_output(output_text, success=success)

        completion_event = AgentCompletionEvent(
            agent_id=self._config.agent_id,
            task_id=self._task_id,
            success=success,
            output=truncated,
            output_path=output_path,
        )
        await self._completion_queue.put(completion_event)

    def _write_output(self, output: str, *, success: bool) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / f"{self._config.agent_id}.output"
        content = (
            f"agent_id: {self._config.agent_id}\n"
            f"task_id: {self._task_id}\n"
            f"status: {'completed' if success else 'failed'}\n"
            f"---\n"
            f"{output}"
        )
        output_path.write_text(content, encoding="utf-8")
        return output_path


def build_worktree_notice(config: AgentConfig, project_root: Path) -> str:
    return (
        f"你继承了父代理在 {project_root} 的对话上下文。\n"
        f"你现在在隔离的 git worktree {config.worktree_path} 中。\n"
        f"上下文中的路径指向父目录——请翻译到你的 worktree。\n"
        f"编辑前请重新读取文件（父代理可能已修改）。"
    )

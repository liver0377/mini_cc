from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mini_cc.models import (
    AgentCompletionEvent,
    AgentConfig,
    AgentHeartbeatEvent,
    AgentStartEvent,
    AgentStatus,
    AgentTextDeltaEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    QueryState,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.runtime.query.engine import QueryEngine
from mini_cc.task.service import TaskService

if TYPE_CHECKING:
    from mini_cc.runtime.agents.bus import AgentEventBus
    from mini_cc.runtime.agents.snapshot import SnapshotService

_HEARTBEAT_INTERVAL_SECONDS = 30.0


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
        snapshot_svc: SnapshotService | None = None,
        event_queue: asyncio.Queue[Event] | None = None,
        version_provider: Callable[[], str] | None = None,
        lifecycle_bus: AgentEventBus | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> None:
        self._config = config
        self._engine = engine
        self._state = state
        self._task_id = task_id
        self._task_service = task_service
        self._completion_queue = completion_queue
        self._output_dir = output_dir
        self.snapshot_svc = snapshot_svc
        self._event_queue = event_queue
        self._version_provider = version_provider
        self._lifecycle_bus = lifecycle_bus
        self._interrupt_event = interrupt_event
        self._status = AgentStatus.CREATED
        self._cancel_event = asyncio.Event()
        self._collected_output: list[str] = []
        self._completed_version_stamp = config.base_version_stamp
        self._background_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._started_at: float = 0.0

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

    @property
    def completed_version_stamp(self) -> str:
        return self._completed_version_stamp

    @property
    def background_task(self) -> asyncio.Task[None] | None:
        return self._background_task

    @background_task.setter
    def background_task(self, value: asyncio.Task[None] | None) -> None:
        self._background_task = value

    async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
        self._status = AgentStatus.RUNNING
        self._started_at = time.monotonic()
        await self._task_service.claim(self._task_id, owner=f"agent-{self._config.agent_id}")
        self._start_heartbeat()

        try:
            async for event in self._engine.submit_message(prompt, self._state):
                if self._cancel_event.is_set():
                    self._status = AgentStatus.CANCELLED
                    await self._task_service.cancel(self._task_id)
                    return
                self._collect_text(event)
                yield event

            await self._finish(success=True)
        except asyncio.CancelledError:
            await self._finish(success=False, error="cancelled")
            raise
        except Exception as e:
            await self._finish(success=False, error=str(e))
            raise
        finally:
            await self._stop_heartbeat()

    async def run_background(self, prompt: str) -> None:
        self._status = AgentStatus.BACKGROUND_RUNNING
        self._started_at = time.monotonic()
        await self._task_service.claim(self._task_id, owner=f"agent-{self._config.agent_id}")
        await self._emit_event(
            AgentStartEvent(agent_id=self._config.agent_id, task_id=self._task_id, prompt=prompt[:80])
        )
        self._start_heartbeat()

        try:
            async for event in self._engine.submit_message(prompt, self._state):
                if self._cancel_event.is_set():
                    self._status = AgentStatus.CANCELLED
                    await self._task_service.cancel(self._task_id)
                    return
                self._collect_text(event)
                self._forward_event(event)
            await self._finish(success=True)
        except asyncio.CancelledError:
            await self._finish(success=False, error="cancelled")
            raise
        except Exception as e:
            await self._finish(success=False, error=str(e))
        finally:
            await self._stop_heartbeat()

    def cancel(self) -> None:
        self._cancel_event.set()
        if self._interrupt_event is not None:
            self._interrupt_event.set()

    async def _emit_event(self, event: Event) -> None:
        if self._event_queue is not None:
            await self._event_queue.put(event)

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        if self._event_queue is None and self._lifecycle_bus is None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._heartbeat_task
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            elapsed_seconds = max(0, int(time.monotonic() - self._started_at))
            event = AgentHeartbeatEvent(
                agent_id=self._config.agent_id,
                task_id=self._task_id,
                elapsed_seconds=elapsed_seconds,
                status=self._status.value,
            )
            await self._emit_event(event)
            if self._lifecycle_bus is not None:
                from mini_cc.runtime.agents.bus import AgentLifecycleEvent

                self._lifecycle_bus.publish_nowait(
                    AgentLifecycleEvent(
                        event_type="heartbeat",
                        agent_id=self._config.agent_id,
                        heartbeat_at=datetime.now(UTC).isoformat(),
                        heartbeat_elapsed_seconds=elapsed_seconds,
                        heartbeat_status=self._status.value,
                    )
                )

    def _forward_event(self, event: Event) -> None:
        if self._event_queue is None:
            return
        agent_id = self._config.agent_id
        if isinstance(event, ToolCallStart):
            self._event_queue.put_nowait(AgentToolCallEvent(agent_id=agent_id, tool_name=event.name))
        elif isinstance(event, ToolResultEvent):
            preview = event.output[:100] + ("..." if len(event.output) > 100 else "")
            self._event_queue.put_nowait(
                AgentToolResultEvent(
                    agent_id=agent_id, tool_name=event.name, success=event.success, output_preview=preview
                )
            )
        elif isinstance(event, TextDelta):
            self._event_queue.put_nowait(AgentTextDeltaEvent(agent_id=agent_id, content=event.content))

    def _collect_text(self, event: Event) -> None:
        if isinstance(event, TextDelta):
            self._collected_output.append(event.content)

    async def _finish(self, *, success: bool, error: str | None = None) -> None:
        output_text = "".join(self._collected_output)
        truncated = output_text[:500]
        termination_reason = "completed" if success else ("cancelled" if self._cancel_event.is_set() else "failed")

        if not success and self.snapshot_svc is not None:
            restored = self.snapshot_svc.restore_all()
            if restored:
                self._collected_output.append(f"\n\n[Agent 失败，已自动回滚以下文件: {', '.join(restored)}]")
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
        if self._version_provider is not None:
            self._completed_version_stamp = self._version_provider()
        else:
            self._completed_version_stamp = self._config.base_version_stamp

        completion_event = AgentCompletionEvent(
            agent_id=self._config.agent_id,
            task_id=self._task_id,
            success=success,
            output=truncated,
            output_path=output_path,
            base_version_stamp=self._config.base_version_stamp,
            completed_version_stamp=self._completed_version_stamp,
            is_stale=self._config.base_version_stamp != self._completed_version_stamp,
        )
        await self._completion_queue.put(completion_event)

        if self._lifecycle_bus is not None:
            event_type = "completed" if success else "cancelled"
            from mini_cc.runtime.agents.bus import AgentLifecycleEvent

            self._lifecycle_bus.publish_nowait(
                AgentLifecycleEvent(
                    event_type=event_type,
                    agent_id=self._config.agent_id,
                    success=success,
                    output_preview=truncated,
                    output_path=output_path,
                    is_stale=self._config.base_version_stamp != self._completed_version_stamp,
                    base_version_stamp=self._config.base_version_stamp,
                    completed_version_stamp=self._completed_version_stamp,
                    termination_reason=termination_reason if error is None else error,
                )
            )

    def _write_output(self, output: str, *, success: bool) -> str:
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
        return str(output_path)


def build_workspace_notice(config: AgentConfig, project_root: Path) -> str:
    return (
        f"你继承了父代理在 {project_root} 的对话上下文。\n"
        f"你直接操作主工作区 {project_root}，无需路径翻译。\n"
        f"编辑前请重新读取文件（父代理可能已修改）。"
    )

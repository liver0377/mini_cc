from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mini_cc.models import (
    AgentBudget,
    AgentCompletionEvent,
    AgentStatus,
    QueryState,
)

if TYPE_CHECKING:
    from mini_cc.context.engine_context import EngineContext
    from mini_cc.runtime.agents import AgentDispatcher, AgentLifecycleEvent, SubAgent


@dataclass(frozen=True)
class AgentRunHandle:
    agent_id: str
    task_id: int
    events: AsyncGenerator[Any, None]


@dataclass(frozen=True)
class BackgroundAgentHandle:
    agent_id: str
    task_id: int
    task: asyncio.Task[None]


@dataclass(frozen=True)
class AgentView:
    agent_id: str
    task_id: int
    status: AgentStatus
    workspace_path: str
    is_fork: bool
    parent_agent_id: str | None
    scope_paths: list[str]
    base_version_stamp: str
    message_count: int
    prompt_preview: str
    output_path: str


class RuntimeFacade:
    def __init__(self, engine_ctx: EngineContext) -> None:
        self._ctx = engine_ctx

    @property
    def engine_ctx(self) -> EngineContext:
        return self._ctx

    async def dispatch_agent(
        self,
        *,
        prompt: str,
        readonly: bool = False,
        fork: bool = False,
        parent_state: QueryState | None = None,
        mode: str = "build",
        scope_paths: list[str] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        work_item_id: str | None = None,
        role: str | None = None,
    ) -> Any:
        from mini_cc.runtime import AgentDispatchRequest

        request = AgentDispatchRequest(
            prompt=prompt,
            readonly=readonly,
            fork=fork,
            parent_state=parent_state,
            mode=mode,
            scope_paths=scope_paths or [],
            run_id=run_id,
            step_id=step_id,
            work_item_id=work_item_id,
            role=role,
        )
        return await self._resolve_dispatcher().dispatch(request)

    async def run_agent(
        self,
        *,
        prompt: str,
        readonly: bool = False,
        fork: bool = False,
        parent_state: QueryState | None = None,
        mode: str = "build",
        scope_paths: list[str] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        work_item_id: str | None = None,
        role: str | None = None,
    ) -> AgentRunHandle:
        agent = await self.dispatch_agent(
            prompt=prompt,
            readonly=readonly,
            fork=fork,
            parent_state=parent_state,
            mode=mode,
            scope_paths=scope_paths,
            run_id=run_id,
            step_id=step_id,
            work_item_id=work_item_id,
            role=role,
        )
        return AgentRunHandle(
            agent_id=agent.config.agent_id,
            task_id=agent.task_id,
            events=agent.run(prompt),
        )

    async def start_background_agent(
        self,
        *,
        prompt: str,
        readonly: bool = True,
        fork: bool = False,
        parent_state: QueryState | None = None,
        mode: str = "plan",
        scope_paths: list[str] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        work_item_id: str | None = None,
        role: str | None = None,
    ) -> BackgroundAgentHandle:
        agent = await self.dispatch_agent(
            prompt=prompt,
            readonly=readonly,
            fork=fork,
            parent_state=parent_state,
            mode=mode,
            scope_paths=scope_paths,
            run_id=run_id,
            step_id=step_id,
            work_item_id=work_item_id,
            role=role,
        )
        task = asyncio.create_task(agent.run_background(prompt))
        agent.background_task = task
        return BackgroundAgentHandle(
            agent_id=agent.config.agent_id,
            task_id=agent.task_id,
            task=task,
        )

    def get_agent(self, agent_id: str) -> SubAgent | None:
        return self._ctx.get_runtime_agent(agent_id)

    def list_agents(self) -> list[AgentView]:
        views: list[AgentView] = []
        for agent in self._ctx.list_runtime_agents():
            prompt_preview = "(无消息)"
            if agent.state.messages:
                last_msg = agent.state.messages[-1]
                if last_msg.content:
                    prompt_preview = last_msg.content[:60]
            output_path = Path(agent.config.workspace_path) / ".mini_cc" / "tasks" / f"{agent.config.agent_id}.output"
            views.append(
                AgentView(
                    agent_id=agent.config.agent_id,
                    task_id=agent.task_id,
                    status=agent.status,
                    workspace_path=agent.config.workspace_path,
                    is_fork=agent.config.is_fork,
                    parent_agent_id=agent.config.parent_agent_id,
                    scope_paths=list(agent.config.scope_paths),
                    base_version_stamp=agent.config.base_version_stamp,
                    message_count=len(agent.state.messages),
                    prompt_preview=prompt_preview,
                    output_path=str(output_path),
                )
            )
        return views

    def cancel_agents(self, agent_ids: list[str] | None = None) -> list[str]:
        return self._ctx.cancel_runtime_agents(agent_ids)

    async def cleanup_agents(self, agent_ids: list[str] | None = None) -> None:
        await self._ctx.cleanup_runtime_agents(agent_ids)

    def set_step_context(self, step_id: str | None) -> None:
        self._ctx.set_runtime_step_context(step_id)

    def drain_lifecycle_events(self) -> list[AgentLifecycleEvent]:
        return self._ctx.drain_lifecycle_events()

    def drain_completion(self, agent_id: str) -> AgentCompletionEvent | None:
        return self._ctx.drain_completion(agent_id)

    def read_agent_output(self, agent_id: str) -> str:
        for view in self.list_agents():
            if view.agent_id != agent_id:
                continue
            try:
                return Path(view.output_path).read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                return ""
        return ""

    @property
    def agent_budget(self) -> AgentBudget | None:
        value = self._ctx.agent_budget
        if value is None:
            return None
        return value if isinstance(value, AgentBudget) else None

    @agent_budget.setter
    def agent_budget(self, value: AgentBudget | None) -> None:
        self._ctx.agent_budget = value

    @property
    def active_agent_count(self) -> int:
        return self._ctx.active_agent_count

    @property
    def has_agent_runtime(self) -> bool:
        return self._ctx.has_agent_runtime

    @property
    def model_name(self) -> str:
        return self._ctx.model_name

    @property
    def working_directory(self) -> str:
        return self._ctx.working_directory

    @property
    def current_run_id(self) -> str | None:
        return self._ctx.current_run_id

    @current_run_id.setter
    def current_run_id(self, value: str | None) -> None:
        self._ctx.current_run_id = value

    def prepare_query_state(self, state: QueryState | None, mode: str) -> QueryState:
        if state is None:
            return self._ctx.new_query_state(mode=mode)
        next_state = state.model_copy(deep=True)
        self._ctx.apply_system_prompt(next_state, mode=mode)
        return next_state

    def new_query_state(self, *, mode: str | None = None, run_id: str | None = None) -> QueryState:
        return self._ctx.new_query_state(mode=mode, run_id=run_id)

    def apply_system_prompt(self, state: QueryState, *, mode: str | None = None, run_id: str | None = None) -> None:
        self._ctx.apply_system_prompt(state, mode=mode, run_id=run_id)

    async def compact_state(self, state: QueryState) -> None:
        await self._ctx.compact_state(state)

    def execution_scope(
        self,
        *,
        run_id: str | None = None,
        mode: str | None = None,
        agent_budget: object | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> AbstractContextManager[None]:
        return self._ctx.execution_scope(
            run_id=run_id,
            mode=mode,
            agent_budget=agent_budget,
            interrupt_event=interrupt_event,
        )

    @property
    def mode(self) -> str:
        return self._ctx.mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._ctx.mode = value

    @property
    def model(self) -> str:
        return self._ctx.model

    def submit_message(self, prompt: str, state: QueryState) -> Any:
        return self._ctx.submit_message(prompt, state)

    def _resolve_dispatcher(self) -> AgentDispatcher:
        def _factory() -> AgentDispatcher:
            manager = self._ctx.get_runtime_manager()
            if manager is None:
                raise RuntimeError("AgentManager is required for agent dispatch")
            from mini_cc.runtime.agents import AgentDispatcher

            return AgentDispatcher(
                manager=manager,
                get_budget=lambda: self.agent_budget,
            )

        return self._ctx.resolve_agent_dispatcher(_factory)

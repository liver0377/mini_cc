from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from mini_cc.context.system_prompt import EnvInfo, SystemPromptBuilder
from mini_cc.models import AgentStatus, Event, Message, QueryState, Role
from mini_cc.runtime.agents import AgentEventBus, AgentManager

if TYPE_CHECKING:
    from mini_cc.runtime.agents import AgentDispatcher
    from mini_cc.runtime.query import QueryEngine

CompactFn = Callable[[list[Message]], Awaitable[str]]
ReplaceSummaryFn = Callable[[QueryState, str], None]


class EngineContext:
    _run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("engine_run_id", default=None)
    _mode_var: contextvars.ContextVar[str] = contextvars.ContextVar("engine_mode", default="build")
    _budget_var: contextvars.ContextVar[object | None] = contextvars.ContextVar("engine_agent_budget", default=None)
    _interrupt_var: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
        "engine_interrupt_event", default=None
    )
    _UNCHANGED = object()

    def __init__(
        self,
        engine: QueryEngine,
        prompt_builder: SystemPromptBuilder,
        env_info: EnvInfo,
        agent_manager: AgentManager | None = None,
        lifecycle_bus: AgentEventBus | None = None,
        completion_queue: asyncio.Queue[Any] | None = None,
        agent_dispatcher: AgentDispatcher | None = None,
        mode: str = "build",
        model: str = "",
        base_interrupt_event: threading.Event | None = None,
        compact_fn: CompactFn | None = None,
        replace_summary_fn: ReplaceSummaryFn | None = None,
    ) -> None:
        self._engine = engine
        self._prompt_builder = prompt_builder
        self._env_info = env_info
        self._agent_manager = agent_manager
        self._lifecycle_bus = lifecycle_bus
        self._completion_queue = completion_queue
        self._agent_dispatcher = agent_dispatcher
        self._model = model
        self._base_interrupt_event = base_interrupt_event
        self._compact_fn = compact_fn
        self._replace_summary_fn = replace_summary_fn
        self._mode_var.set(mode)

    @property
    def prompt_builder(self) -> SystemPromptBuilder:
        return self._prompt_builder

    @property
    def env_info(self) -> EnvInfo:
        return self._env_info

    @property
    def model(self) -> str:
        return self._model

    @property
    def mode(self) -> str:
        return self._mode_var.get()

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode_var.set(value)

    @property
    def current_run_id(self) -> str | None:
        return self._run_id_var.get()

    @current_run_id.setter
    def current_run_id(self, value: str | None) -> None:
        self._run_id_var.set(value)

    @property
    def agent_budget(self) -> object | None:
        return self._budget_var.get()

    @agent_budget.setter
    def agent_budget(self, value: object | None) -> None:
        self._budget_var.set(value)

    @property
    def is_interrupted(self) -> bool:
        scoped = self._interrupt_var.get()
        return (self._base_interrupt_event is not None and self._base_interrupt_event.is_set()) or (
            scoped is not None and scoped.is_set()
        )

    @property
    def active_agent_count(self) -> int:
        if self._agent_manager is None:
            return 0
        return sum(
            1
            for a in self._agent_manager.agents.values()
            if a.status in (AgentStatus.CREATED, AgentStatus.RUNNING, AgentStatus.BACKGROUND_RUNNING)
        )

    @property
    def model_name(self) -> str:
        return self._env_info.model_name

    @property
    def working_directory(self) -> str:
        return self._env_info.working_directory

    def build_system_prompt(self, *, mode: str | None = None, run_id: str | None = None) -> str:
        return self.prompt_builder.build(
            self._env_info,
            mode=mode or self.mode,
            run_id=self.current_run_id if run_id is None else run_id,
        )

    def new_query_state(self, *, mode: str | None = None, run_id: str | None = None) -> QueryState:
        return QueryState(
            messages=[Message(role=Role.SYSTEM, content=self.build_system_prompt(mode=mode, run_id=run_id))]
        )

    def apply_system_prompt(
        self,
        state: QueryState,
        *,
        mode: str | None = None,
        run_id: str | None = None,
    ) -> None:
        content = self.build_system_prompt(mode=mode, run_id=run_id)
        if state.messages and state.messages[0].role == Role.SYSTEM:
            state.messages[0] = Message(role=Role.SYSTEM, content=content)
        else:
            state.messages.insert(0, Message(role=Role.SYSTEM, content=content))

    def submit_message(self, prompt: str, state: QueryState) -> AsyncGenerator[Event, None]:
        return self._engine.submit_message(prompt, state)

    def replace_engine(self, engine: QueryEngine) -> None:
        self._engine = engine

    @property
    def has_agent_runtime(self) -> bool:
        return self._agent_manager is not None

    def list_runtime_agents(self) -> list[Any]:
        if self._agent_manager is None:
            return []
        return list(self._agent_manager.agents.values())

    def get_runtime_agent(self, agent_id: str) -> Any | None:
        if self._agent_manager is None:
            return None
        return self._agent_manager.get_agent(agent_id)

    def get_runtime_manager(self) -> AgentManager | None:
        return self._agent_manager

    def cancel_runtime_agents(self, agent_ids: list[str] | None = None) -> list[str]:
        if self._agent_manager is None:
            return []
        return self._agent_manager.cancel_agents(agent_ids)

    async def cleanup_runtime_agents(self, agent_ids: list[str] | None = None) -> None:
        if self._agent_manager is None:
            return
        target_ids = agent_ids or list(self._agent_manager.agents.keys())
        for agent_id in target_ids:
            await self._agent_manager.cleanup(agent_id)

    def set_runtime_step_context(self, step_id: str | None) -> None:
        if self._agent_manager is None:
            return
        if step_id is None:
            self._agent_manager.clear_current_step()
        else:
            self._agent_manager.set_current_step(step_id)

    def drain_lifecycle_events(self) -> list[Any]:
        if self._lifecycle_bus is None:
            return []
        return self._lifecycle_bus.drain()

    def drain_completion(self, agent_id: str) -> Any | None:
        if self._completion_queue is None:
            return None
        drained: list[Any] = []
        matched: Any | None = None
        while not self._completion_queue.empty():
            try:
                event = self._completion_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if getattr(event, "agent_id", None) == agent_id and matched is None:
                matched = event
            else:
                drained.append(event)
        for event in drained:
            self._completion_queue.put_nowait(event)
        return matched

    def resolve_agent_dispatcher(
        self,
        factory: Callable[[], AgentDispatcher],
    ) -> AgentDispatcher:
        if self._agent_dispatcher is not None:
            return self._agent_dispatcher
        dispatcher = factory()
        self._agent_dispatcher = dispatcher
        return dispatcher

    def configure_runtime(
        self,
        *,
        agent_manager: Any = _UNCHANGED,
        lifecycle_bus: Any = _UNCHANGED,
        completion_queue: Any = _UNCHANGED,
        agent_dispatcher: Any = _UNCHANGED,
    ) -> None:
        if agent_manager is not self._UNCHANGED:
            self._agent_manager = agent_manager
        if lifecycle_bus is not self._UNCHANGED:
            self._lifecycle_bus = lifecycle_bus
        if completion_queue is not self._UNCHANGED:
            self._completion_queue = completion_queue
        if agent_dispatcher is not self._UNCHANGED:
            self._agent_dispatcher = agent_dispatcher

    async def compact_state(self, state: QueryState) -> None:
        if self._compact_fn is None or self._replace_summary_fn is None:
            raise RuntimeError("Context compaction is not configured")
        summary = await self._compact_fn(state.messages)
        self._replace_summary_fn(state, summary)

    @contextmanager
    def execution_scope(
        self,
        *,
        run_id: str | None = None,
        mode: str | None = None,
        agent_budget: object | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> Iterator[None]:
        run_token = self._run_id_var.set(run_id if run_id is not None else self._run_id_var.get())
        mode_token = self._mode_var.set(mode if mode is not None else self._mode_var.get())
        budget_token = self._budget_var.set(agent_budget if agent_budget is not None else self._budget_var.get())
        interrupt_token = self._interrupt_var.set(interrupt_event)
        try:
            yield
        finally:
            self._run_id_var.reset(run_token)
            self._mode_var.reset(mode_token)
            self._budget_var.reset(budget_token)
            self._interrupt_var.reset(interrupt_token)

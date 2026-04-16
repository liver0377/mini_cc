from __future__ import annotations

import asyncio
import contextvars
import os
import secrets
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rich import print as rprint

from mini_cc.agent.bus import AgentEventBus
from mini_cc.agent.manager import AgentManager
from mini_cc.context.system_prompt import EnvInfo, SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.memory.extractor import MemoryExtractor
from mini_cc.models import AgentCompletionEvent, AgentStatus, Event, QueryState
from mini_cc.query_engine.engine import QueryEngine
from mini_cc.task.service import TaskService
from mini_cc.tool_executor.executor import StreamingToolExecutor
from mini_cc.tools import create_default_registry
from mini_cc.tools.agent_tool import AgentTool


class EngineContext:
    _run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("engine_run_id", default=None)
    _mode_var: contextvars.ContextVar[str] = contextvars.ContextVar("engine_mode", default="build")
    _budget_var: contextvars.ContextVar[object | None] = contextvars.ContextVar("engine_agent_budget", default=None)
    _interrupt_var: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
        "engine_interrupt_event", default=None
    )

    def __init__(
        self,
        engine: QueryEngine,
        prompt_builder: SystemPromptBuilder,
        env_info: EnvInfo,
        agent_manager: AgentManager | None = None,
        lifecycle_bus: AgentEventBus | None = None,
        completion_queue: asyncio.Queue[AgentCompletionEvent] | None = None,
        mode: str = "build",
        model: str = "",
        base_interrupt_event: threading.Event | None = None,
    ) -> None:
        self.engine = engine
        self.prompt_builder = prompt_builder
        self.env_info = env_info
        self.agent_manager = agent_manager
        self.lifecycle_bus = lifecycle_bus
        self.completion_queue = completion_queue
        self.model = model
        self._base_interrupt_event = base_interrupt_event
        self._mode_var.set(mode)

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
        if self.agent_manager is None:
            return 0
        return sum(
            1
            for a in self.agent_manager.agents.values()
            if a.status in (AgentStatus.CREATED, AgentStatus.RUNNING, AgentStatus.BACKGROUND_RUNNING)
        )

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


def load_dotenv() -> None:
    try:
        from dotenv import load_dotenv as _load

        project_root = Path(__file__).resolve().parents[3]
        dotenv_path = project_root / ".env"
        _load(dotenv_path=dotenv_path, override=True)
    except ImportError:
        pass


class _EngineConfig:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://api.openai.com/v1"
        self.model = model or "gpt-4o"

    @classmethod
    def from_env(cls) -> _EngineConfig:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        model = os.environ.get("OPENAI_MODEL") or None
        return cls(api_key=api_key, base_url=base_url, model=model)


def create_engine(
    config: _EngineConfig | None = None,
    *,
    interrupted_event: threading.Event | None = None,
) -> EngineContext:
    if config is None:
        load_dotenv()
        config = _EngineConfig.from_env()

    if not config.api_key:
        rprint("[bold red]错误:[/] 未设置 OPENAI_API_KEY 环境变量")
        rprint("[dim]请在 .env 文件或环境变量中设置 OPENAI_API_KEY[/]")
        sys.exit(1)

    from mini_cc.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
    )

    completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
    agent_event_queue: asyncio.Queue[Event] = asyncio.Queue()
    lifecycle_bus = AgentEventBus()

    registry = create_default_registry()
    executor = StreamingToolExecutor(registry, is_interrupted=lambda: interrupt_flag.is_set())

    interrupt_flag = interrupted_event or threading.Event()

    tool_use_ctx = ToolUseContext(
        get_schemas=registry.to_api_format,
        execute=executor.run,
        is_interrupted=lambda: interrupt_flag.is_set() or (ctx_ref[0].is_interrupted if ctx_ref else False),
    )

    memory_extractor = MemoryExtractor(stream_fn=provider.stream, cwd=str(Path.cwd()))

    async def _post_turn_hook(state: QueryState) -> None:
        if memory_extractor.should_extract(state):
            memory_extractor.fire_and_forget(state)

    engine = QueryEngine(
        stream_fn=provider.stream,
        tool_use_ctx=tool_use_ctx,
        completion_queue=completion_queue,
        agent_event_queue=agent_event_queue,
        post_turn_hook=_post_turn_hook,
        model=config.model,
    )

    session_id = secrets.token_hex(4)
    task_service = TaskService(task_list_id=session_id)
    env_info = collect_env_info(config.model)
    prompt_builder = SystemPromptBuilder()

    ctx_ref: list[EngineContext] = []

    agent_manager = AgentManager(
        project_root=Path.cwd(),
        stream_fn=provider.stream,
        task_service=task_service,
        completion_queue=completion_queue,
        agent_event_queue=agent_event_queue,
        prompt_builder=prompt_builder,
        env_info=env_info,
        lifecycle_bus=lifecycle_bus,
    )

    engine_ctx = EngineContext(
        engine=engine,
        prompt_builder=prompt_builder,
        env_info=env_info,
        agent_manager=agent_manager,
        lifecycle_bus=lifecycle_bus,
        completion_queue=completion_queue,
        model=config.model,
        base_interrupt_event=interrupt_flag,
    )
    ctx_ref.append(engine_ctx)

    engine._active_agents_fn = lambda: ctx_ref[0].active_agent_count if ctx_ref else 0

    agent_tool = AgentTool(
        manager=agent_manager,
        get_parent_state=lambda: engine.state if engine.state else QueryState(),
        event_queue=agent_event_queue,
        get_mode=lambda: ctx_ref[0].mode,
        get_budget=lambda: ctx_ref[0].agent_budget if ctx_ref else None,
    )
    registry.register(agent_tool)

    return engine_ctx

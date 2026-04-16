from __future__ import annotations

import asyncio
import os
import secrets
import sys
import threading
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
    ) -> None:
        self.engine = engine
        self.prompt_builder = prompt_builder
        self.env_info = env_info
        self.agent_manager = agent_manager
        self.lifecycle_bus = lifecycle_bus
        self.completion_queue = completion_queue
        self._mode = mode
        self.model = model
        self.current_run_id: str | None = None
        self.agent_budget: object | None = None

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value

    @property
    def active_agent_count(self) -> int:
        if self.agent_manager is None:
            return 0
        return sum(
            1
            for a in self.agent_manager.agents.values()
            if a.status in (AgentStatus.CREATED, AgentStatus.RUNNING, AgentStatus.BACKGROUND_RUNNING)
        )


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
    executor = StreamingToolExecutor(registry)

    interrupt_flag = interrupted_event or threading.Event()

    tool_use_ctx = ToolUseContext(
        get_schemas=registry.to_api_format,
        execute=executor.run,
        is_interrupted=interrupt_flag.is_set,
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

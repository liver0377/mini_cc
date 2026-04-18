from __future__ import annotations

import asyncio
import secrets
import threading
from pathlib import Path
from typing import Any, cast

from mini_cc.context.engine_context import EngineContext
from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.models import AgentBudget, AgentCompletionEvent, Event, QueryState
from mini_cc.runtime.agents import AgentDispatcher, AgentEventBus, AgentManager
from mini_cc.runtime.execution.factories import (
    ProviderFactory,
    ToolingFactory,
    _EngineConfig,
    _validate_config,
    load_dotenv,
)
from mini_cc.runtime.query import QueryEngine
from mini_cc.task.service import TaskService
from mini_cc.tools.agent_tool import AgentTool


def create_engine(
    config: _EngineConfig | None = None,
    *,
    interrupted_event: threading.Event | None = None,
) -> EngineContext:
    if config is None:
        load_dotenv()
        config = _EngineConfig.from_env()

    _validate_config(config)

    provider = ProviderFactory.create(config)

    completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
    agent_event_queue: asyncio.Queue[Event] = asyncio.Queue()
    lifecycle_bus = AgentEventBus()

    interrupt_flag = interrupted_event or threading.Event()

    def _is_interrupted() -> bool:
        return interrupt_flag.is_set() or (ctx_ref[0].is_interrupted if ctx_ref else False)

    registry, executor = ToolingFactory.create_default(is_interrupted=_is_interrupted)

    tool_use_ctx = ToolUseContext(
        get_schemas=registry.to_api_format,
        execute=executor.run,
        is_interrupted=_is_interrupted,
    )

    from mini_cc.features.compression.compressor import compress_messages, replace_with_summary, should_auto_compact
    from mini_cc.features.memory.extractor import MemoryExtractor
    from mini_cc.features.memory.store import load_memory_index

    memory_extractor = MemoryExtractor(stream_fn=provider.stream, cwd=str(Path.cwd()))

    async def _post_turn_hook(state: QueryState) -> None:
        if memory_extractor.should_extract(state):
            memory_extractor.fire_and_forget(state)

    stream_fn: Any = provider.stream

    engine = QueryEngine(
        stream_fn=stream_fn,
        tool_use_ctx=tool_use_ctx,
        completion_queue=completion_queue,
        agent_event_queue=agent_event_queue,
        post_turn_hook=_post_turn_hook,
        model=config.model,
        active_agents_fn=lambda: ctx_ref[0].active_agent_count if ctx_ref else 0,
        compact_fn=lambda msgs: compress_messages(msgs, provider.stream, config.model),
        should_compact_fn=lambda msgs: should_auto_compact(msgs, config.model),
        replace_summary_fn=replace_with_summary,
    )

    session_id = secrets.token_hex(4)
    task_service = TaskService(task_list_id=session_id)
    env_info = collect_env_info(config.model)
    prompt_builder = SystemPromptBuilder(memory_loader=load_memory_index)

    ctx_ref: list[EngineContext] = []

    agent_manager = AgentManager(
        project_root=Path.cwd(),
        stream_fn=stream_fn,
        task_service=task_service,
        completion_queue=completion_queue,
        agent_event_queue=agent_event_queue,
        prompt_builder=prompt_builder,
        env_info=env_info,
        lifecycle_bus=lifecycle_bus,
    )

    agent_dispatcher = AgentDispatcher(
        manager=agent_manager,
        get_budget=lambda: cast(AgentBudget | None, ctx_ref[0].agent_budget if ctx_ref else None),
    )

    engine_ctx = EngineContext(
        engine=engine,
        prompt_builder=prompt_builder,
        env_info=env_info,
        agent_manager=agent_manager,
        lifecycle_bus=lifecycle_bus,
        completion_queue=completion_queue,
        agent_dispatcher=agent_dispatcher,
        model=config.model,
        base_interrupt_event=interrupt_flag,
        compact_fn=lambda msgs: compress_messages(msgs, provider.stream, config.model),
        replace_summary_fn=replace_with_summary,
    )
    ctx_ref.append(engine_ctx)

    agent_tool = AgentTool(
        manager=agent_manager,
        dispatcher=agent_dispatcher,
        get_parent_state=lambda: engine.state if engine.state else QueryState(),
        event_queue=agent_event_queue,
        get_mode=lambda: ctx_ref[0].mode,
        get_run_id=lambda: ctx_ref[0].current_run_id,
    )
    registry.register(agent_tool)

    return engine_ctx

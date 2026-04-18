from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from mini_cc.context.engine_context import EngineContext
from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.models import AgentCompletionEvent, Event, Message, Role, TextDelta, ToolCall, ToolResultEvent
from mini_cc.runtime.agents.bus import AgentEventBus, AgentLifecycleEvent
from mini_cc.runtime.facade import RuntimeFacade
from mini_cc.runtime.query import QueryEngine


async def _noop_execute(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
    return
    yield


async def _stream_text(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
    yield TextDelta(content="test response")


class _FakeAgent:
    def __init__(self, agent_id: str = "a1", task_id: int = 1) -> None:
        self.config = type("Config", (), {"agent_id": agent_id})()
        self.task_id = task_id

    async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
        yield TextDelta(content=f"run:{prompt}")

    async def run_background(self, prompt: str) -> None:
        await asyncio.sleep(0)


class _FakeDispatcher:
    def __init__(self, agent: _FakeAgent | None = None) -> None:
        self._agent = agent or _FakeAgent()

    async def dispatch(self, request: object) -> _FakeAgent:
        return self._agent


def _make_engine_ctx(tmp_path) -> EngineContext:
    engine = QueryEngine(
        stream_fn=_stream_text,
        tool_use_ctx=ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
        ),
        model="test-model",
    )
    env_info = collect_env_info("test-model", cwd=tmp_path)
    return EngineContext(
        engine=engine,
        prompt_builder=SystemPromptBuilder(),
        env_info=env_info,
        lifecycle_bus=AgentEventBus(),
        model="test-model",
    )


class TestRuntimeFacadeDrainLifecycle:
    def test_drain_lifecycle_returns_empty_when_no_bus(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        engine_ctx.configure_runtime(lifecycle_bus=None)
        facade = RuntimeFacade(engine_ctx)
        assert facade.drain_lifecycle_events() == []

    def test_drain_lifecycle_returns_events(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        bus = AgentEventBus()
        engine_ctx.configure_runtime(lifecycle_bus=bus)
        event = AgentLifecycleEvent(event_type="created", agent_id="a1", readonly=True)
        bus.publish_nowait(event)
        facade = RuntimeFacade(engine_ctx)
        result = facade.drain_lifecycle_events()
        assert len(result) == 1
        assert result[0].agent_id == "a1"
        assert facade.drain_lifecycle_events() == []

    def test_drain_lifecycle_returns_empty_when_bus_is_empty(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        assert facade.drain_lifecycle_events() == []


class TestRuntimeFacadeCompletion:
    async def test_drain_completion_returns_matching_event(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        engine_ctx.configure_runtime(completion_queue=queue)
        event = AgentCompletionEvent(agent_id="a1", task_id="t1", output="done", success=True, output_path="")
        await queue.put(event)
        facade = RuntimeFacade(engine_ctx)
        result = facade.drain_completion("a1")
        assert result is not None
        assert result.agent_id == "a1"
        assert facade.drain_completion("a1") is None

    async def test_drain_completion_preserves_non_matching(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
        engine_ctx.configure_runtime(completion_queue=queue)
        e1 = AgentCompletionEvent(agent_id="a1", task_id="t1", output="done", success=True, output_path="")
        e2 = AgentCompletionEvent(agent_id="a2", task_id="t2", output="done", success=True, output_path="")
        await queue.put(e1)
        await queue.put(e2)
        facade = RuntimeFacade(engine_ctx)
        result = facade.drain_completion("a1")
        assert result is not None
        assert result.agent_id == "a1"
        remaining = facade.drain_completion("a2")
        assert remaining is not None
        assert remaining.agent_id == "a2"

    def test_drain_completion_returns_none_when_no_queue(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        engine_ctx.configure_runtime(completion_queue=None)
        facade = RuntimeFacade(engine_ctx)
        assert facade.drain_completion("a1") is None


class TestRuntimeFacadeBudget:
    def test_agent_budget_getter_setter(self, tmp_path) -> None:
        from mini_cc.models import AgentBudget

        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        assert facade.agent_budget is None
        budget = AgentBudget(remaining_readonly=3)
        facade.agent_budget = budget
        assert facade.agent_budget is not None
        assert facade.agent_budget.remaining_readonly == 3


class TestRuntimeFacadeStepContext:
    def test_set_step_context_with_no_manager(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        engine_ctx.configure_runtime(agent_manager=None)
        facade = RuntimeFacade(engine_ctx)
        facade.set_step_context("step-1")
        facade.set_step_context(None)


class TestRuntimeFacadePrepareQueryState:
    def test_prepare_query_state_creates_fresh(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        state = facade.prepare_query_state(None, "build")
        assert len(state.messages) == 1
        assert state.messages[0].role == Role.SYSTEM

    def test_prepare_query_state_replaces_system_message(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        existing = facade.prepare_query_state(None, "build")
        updated = facade.prepare_query_state(existing, "plan")
        assert len(updated.messages) == 1
        assert updated.messages[0].role == Role.SYSTEM


class TestRuntimeFacadeProperties:
    def test_mode_property(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        assert facade.mode in {"build", "plan"}

    def test_model_property(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        assert facade.model == "test-model"

    def test_engine_ctx_property(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        assert facade.engine_ctx is engine_ctx

    def test_active_agent_count_zero(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        facade = RuntimeFacade(engine_ctx)
        assert facade.active_agent_count == 0


class TestRuntimeFacadeCancelAgents:
    def test_cancel_agents_with_no_manager(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        engine_ctx.configure_runtime(agent_manager=None)
        facade = RuntimeFacade(engine_ctx)
        assert facade.cancel_agents() == []


class TestRuntimeFacadeAgentExecution:
    async def test_run_agent_returns_stable_handle(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        engine_ctx.configure_runtime(agent_dispatcher=_FakeDispatcher(_FakeAgent(agent_id="run-1", task_id=7)))
        facade = RuntimeFacade(engine_ctx)

        handle = await facade.run_agent(prompt="hello", readonly=True, mode="plan")

        assert handle.agent_id == "run-1"
        assert handle.task_id == 7
        events = [event async for event in handle.events]
        assert len(events) == 1
        assert isinstance(events[0], TextDelta)

    async def test_start_background_agent_returns_task_handle(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        engine_ctx.configure_runtime(agent_dispatcher=_FakeDispatcher(_FakeAgent(agent_id="bg-1", task_id=9)))
        facade = RuntimeFacade(engine_ctx)

        handle = await facade.start_background_agent(prompt="inspect", readonly=True, mode="plan")

        assert handle.agent_id == "bg-1"
        assert handle.task_id == 9
        await handle.task

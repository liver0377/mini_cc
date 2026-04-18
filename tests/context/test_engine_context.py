from __future__ import annotations

from collections.abc import AsyncGenerator

from mini_cc.context.engine_context import EngineContext
from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.models import Event, Message, QueryState, Role, TextDelta, ToolCall, ToolResultEvent
from mini_cc.runtime.query import QueryEngine


async def _noop_execute(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
    return
    yield


async def _stream_text(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
    yield TextDelta(content="ok")


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
        model="test-model",
        compact_fn=_fake_compact,
        replace_summary_fn=_replace_summary,
    )


async def _fake_compact(messages: list[Message]) -> str:
    return "summary"


def _replace_summary(state: QueryState, summary: str) -> None:
    state.messages = [Message(role=Role.USER, content=summary)]


class TestEngineContextPromptOps:
    def test_build_system_prompt_uses_mode(self, tmp_path) -> None:
        ctx = _make_engine_ctx(tmp_path)
        prompt = ctx.build_system_prompt(mode="plan")
        assert "Mode: plan" in prompt

    def test_new_query_state_contains_system_message(self, tmp_path) -> None:
        ctx = _make_engine_ctx(tmp_path)
        state = ctx.new_query_state(mode="build")
        assert len(state.messages) == 1
        assert state.messages[0].role == Role.SYSTEM

    def test_apply_system_prompt_replaces_first_system_message(self, tmp_path) -> None:
        ctx = _make_engine_ctx(tmp_path)
        state = QueryState(messages=[Message(role=Role.SYSTEM, content="old"), Message(role=Role.USER, content="u")])
        ctx.apply_system_prompt(state, mode="plan")
        assert state.messages[0].role == Role.SYSTEM
        assert "Mode: plan" in (state.messages[0].content or "")
        assert state.messages[1].content == "u"


class TestEngineContextCompact:
    async def test_compact_state_uses_configured_callbacks(self, tmp_path) -> None:
        ctx = _make_engine_ctx(tmp_path)
        state = QueryState(messages=[Message(role=Role.USER, content="hello")])
        await ctx.compact_state(state)
        assert len(state.messages) == 1
        assert state.messages[0].content == "summary"

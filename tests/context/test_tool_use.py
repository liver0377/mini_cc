from __future__ import annotations

from collections.abc import AsyncGenerator

from mini_cc.context.tool_use import ToolUseContext
from mini_cc.query_engine.state import ToolCall, ToolResultEvent


class TestGetToolSchemas:
    def test_delegates_to_callback(self) -> None:
        schemas = [{"name": "bash"}, {"name": "file_read"}]
        ctx = ToolUseContext(
            get_schemas=lambda: schemas,
            execute=_noop_execute,
        )
        assert ctx.get_tool_schemas() == schemas

    def test_empty_schemas(self) -> None:
        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
        )
        assert ctx.get_tool_schemas() == []


class TestExecute:
    async def test_delegates_to_callback(self) -> None:
        async def _execute(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
            for tc in tool_calls:
                yield ToolResultEvent(tool_call_id=tc.id, name=tc.name, output="ok", success=True)

        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_execute,
        )
        calls = [ToolCall(id="tc_1", name="bash", arguments='{"cmd":"ls"}')]
        results = [r async for r in ctx.execute(calls)]

        assert len(results) == 1
        assert results[0].output == "ok"

    async def test_empty_tool_calls(self) -> None:
        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
        )
        results = [r async for r in ctx.execute([])]
        assert results == []


class TestCheckPermission:
    def test_default_allows_all(self) -> None:
        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
        )
        assert ctx.check_permission("bash") is True
        assert ctx.check_permission("file_read") is True

    def test_custom_check(self) -> None:
        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
            check_permission=lambda name: name == "file_read",
        )
        assert ctx.check_permission("file_read") is True
        assert ctx.check_permission("bash") is False


class TestIsInterrupted:
    def test_default_not_interrupted(self) -> None:
        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
        )
        assert ctx.is_interrupted is False

    def test_custom_interrupted(self) -> None:
        flag = False

        def _check() -> bool:
            return flag

        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
            is_interrupted=_check,
        )
        assert ctx.is_interrupted is False
        flag = True
        assert ctx.is_interrupted is True


class TestTrace:
    def test_on_trace_called(self) -> None:
        traces: list[tuple[str, dict]] = []

        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
            on_trace=lambda event, kwargs: traces.append((event, kwargs)),
        )
        ctx.trace("stream_start", turn=0)
        ctx.trace("stream_end", turn=1)

        assert len(traces) == 2
        assert traces[0] == ("stream_start", {"turn": 0})
        assert traces[1] == ("stream_end", {"turn": 1})

    def test_default_trace_no_error(self) -> None:
        ctx = ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
        )
        ctx.trace("any_event", key="value")


async def _noop_execute(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
    return
    yield

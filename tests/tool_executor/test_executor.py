from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from mini_cc.query_engine.state import ToolCall
from mini_cc.tool_executor.executor import StreamingToolExecutor
from mini_cc.tools.base import BaseTool, ToolRegistry, ToolResult


class _SafeInput(BaseModel):
    value: str


class _SafeTool(BaseTool):
    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return "Safe tool"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _SafeInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = _SafeInput.model_validate(kwargs)
        return ToolResult(output=f"safe:{parsed.value}")


class _UnsafeTool(BaseTool):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Unsafe tool"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _SafeInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = _SafeInput.model_validate(kwargs)
        return ToolResult(output=f"unsafe:{parsed.value}")


def _make_registry(*tools: BaseTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


class TestUnknownTool:
    async def test_unknown_tool_returns_error(self) -> None:
        executor = StreamingToolExecutor(_make_registry())
        calls = [ToolCall(id="tc_1", name="nonexistent", arguments="{}")]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is False
        assert "Unknown tool" in results[0].output


class TestInvalidJson:
    async def test_invalid_json_returns_error(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_SafeTool()))
        calls = [ToolCall(id="tc_1", name="file_read", arguments="not json")]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is False
        assert "Invalid JSON" in results[0].output


class TestSafeToolExecution:
    async def test_single_safe_tool(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_SafeTool()))
        calls = [ToolCall(id="tc_1", name="file_read", arguments='{"value":"hello"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "safe:hello"


class TestUnsafeToolExecution:
    async def test_single_unsafe_tool(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_UnsafeTool()))
        calls = [ToolCall(id="tc_1", name="bash", arguments='{"value":"ls"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "unsafe:ls"


class TestMixedSafeUnsafe:
    async def test_all_tools_complete(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_SafeTool(), _UnsafeTool()))
        calls = [
            ToolCall(id="tc_1", name="file_read", arguments='{"value":"a"}'),
            ToolCall(id="tc_2", name="bash", arguments='{"value":"b"}'),
        ]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 2
        assert all(r.success for r in results)

    async def test_safe_results_before_unsafe(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_SafeTool(), _UnsafeTool()))
        calls = [
            ToolCall(id="tc_1", name="file_read", arguments='{"value":"a"}'),
            ToolCall(id="tc_2", name="bash", arguments='{"value":"b"}'),
        ]
        results = [r async for r in executor.run(calls)]

        assert results[0].name == "file_read"
        assert results[1].name == "bash"


class TestConcurrentSafeTools:
    async def test_multiple_safe_tools_all_complete(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_SafeTool()))
        calls = [
            ToolCall(id="tc_1", name="file_read", arguments='{"value":"a"}'),
            ToolCall(id="tc_2", name="file_read", arguments='{"value":"b"}'),
        ]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 2
        assert all(r.success for r in results)
        outputs = {r.output for r in results}
        assert outputs == {"safe:a", "safe:b"}


class TestMultipleErrors:
    async def test_mixed_errors_and_success(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_SafeTool()))
        calls = [
            ToolCall(id="tc_1", name="nonexistent", arguments="{}"),
            ToolCall(id="tc_2", name="file_read", arguments="bad json"),
            ToolCall(id="tc_3", name="file_read", arguments='{"value":"ok"}'),
        ]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 3
        assert results[0].success is False
        assert results[1].success is False
        assert results[2].success is True
        assert results[2].output == "safe:ok"


class TestPreExecuteHook:
    async def test_hook_called_before_execution(self) -> None:
        hook_calls: list[tuple[str, dict[str, Any]]] = []

        def hook(tool_name: str, args: dict[str, Any]) -> None:
            hook_calls.append((tool_name, args))

        executor = StreamingToolExecutor(_make_registry(_SafeTool()), pre_execute_hook=hook)
        calls = [ToolCall(id="tc_1", name="file_read", arguments='{"value":"hello"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "safe:hello"
        assert len(hook_calls) == 1
        assert hook_calls[0][0] == "file_read"
        assert hook_calls[0][1] == {"value": "hello"}

    async def test_hook_none_works(self) -> None:
        executor = StreamingToolExecutor(_make_registry(_SafeTool()), pre_execute_hook=None)
        calls = [ToolCall(id="tc_1", name="file_read", arguments='{"value":"test"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is True

    async def test_hook_not_called_for_unknown_tool(self) -> None:
        hook_calls: list[tuple[str, dict[str, Any]]] = []

        def hook(tool_name: str, args: dict[str, Any]) -> None:
            hook_calls.append((tool_name, args))

        executor = StreamingToolExecutor(_make_registry(), pre_execute_hook=hook)
        calls = [ToolCall(id="tc_1", name="nonexistent", arguments="{}")]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is False
        assert len(hook_calls) == 0

    async def test_hook_called_for_unsafe_tool(self) -> None:
        hook_calls: list[tuple[str, dict[str, Any]]] = []

        def hook(tool_name: str, args: dict[str, Any]) -> None:
            hook_calls.append((tool_name, args))

        executor = StreamingToolExecutor(_make_registry(_UnsafeTool()), pre_execute_hook=hook)
        calls = [ToolCall(id="tc_1", name="bash", arguments='{"value":"ls"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is True
        assert len(hook_calls) == 1
        assert hook_calls[0][0] == "bash"

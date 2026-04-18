from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from mini_cc.models import ToolCall
from mini_cc.runtime.execution import ExecutionPolicy, StreamingToolExecutor
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


class TestExecutionPolicyIntegration:
    async def test_readonly_policy_blocks_file_edit(self) -> None:
        class _FileEditTool(BaseTool):
            @property
            def name(self) -> str:
                return "file_edit"

            @property
            def description(self) -> str:
                return "edit"

            @property
            def input_schema(self) -> type[BaseModel]:
                return _SafeInput

            def execute(self, **kwargs: Any) -> ToolResult:
                return ToolResult(output="edited")

        registry = _make_registry(_FileEditTool())
        policy = ExecutionPolicy(readonly=True)
        executor = StreamingToolExecutor(registry, policy=policy)
        calls = [ToolCall(id="tc_1", name="file_edit", arguments='{"value":"test"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is False
        assert "只读" in results[0].output

    async def test_readonly_policy_blocks_file_write(self) -> None:
        class _FileWriteTool(BaseTool):
            @property
            def name(self) -> str:
                return "file_write"

            @property
            def description(self) -> str:
                return "write"

            @property
            def input_schema(self) -> type[BaseModel]:
                return _SafeInput

            def execute(self, **kwargs: Any) -> ToolResult:
                return ToolResult(output="written")

        registry = _make_registry(_FileWriteTool())
        policy = ExecutionPolicy(readonly=True)
        executor = StreamingToolExecutor(registry, policy=policy)
        calls = [ToolCall(id="tc_1", name="file_write", arguments='{"value":"test"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is False

    async def test_scope_policy_blocks_out_of_scope_write(self) -> None:
        class _FileEditTool(BaseTool):
            @property
            def name(self) -> str:
                return "file_edit"

            @property
            def description(self) -> str:
                return "edit"

            @property
            def input_schema(self) -> type[BaseModel]:
                return _SafeInput

            def execute(self, **kwargs: Any) -> ToolResult:
                return ToolResult(output="edited")

        registry = _make_registry(_FileEditTool())
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        executor = StreamingToolExecutor(registry, policy=policy)
        calls = [ToolCall(id="tc_1", name="file_edit", arguments='{"value":"test","file_path":"/project/tests/a.py"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is False
        assert "scope" in results[0].output

    async def test_scope_policy_allows_in_scope_write(self) -> None:
        class _FileEditTool(BaseTool):
            @property
            def name(self) -> str:
                return "file_edit"

            @property
            def description(self) -> str:
                return "edit"

            @property
            def input_schema(self) -> type[BaseModel]:
                return _SafeInput

            def execute(self, **kwargs: Any) -> ToolResult:
                return ToolResult(output="edited")

        registry = _make_registry(_FileEditTool())
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        executor = StreamingToolExecutor(registry, policy=policy)
        calls = [ToolCall(id="tc_1", name="file_edit", arguments='{"value":"test","file_path":"/project/src/a.py"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is True

    async def test_scope_policy_blocks_bash_when_scope_restricted(self) -> None:
        registry = _make_registry(_UnsafeTool())
        policy = ExecutionPolicy(
            scope_paths=["src"],
            workspace_root="/project",
        )
        executor = StreamingToolExecutor(registry, policy=policy)
        calls = [ToolCall(id="tc_1", name="bash", arguments='{"value":"pytest","command":"pytest"}')]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 1
        assert results[0].success is False
        assert "bash" in results[0].output

    async def test_no_policy_allows_all(self) -> None:
        registry = _make_registry(_SafeTool(), _UnsafeTool())
        executor = StreamingToolExecutor(registry)
        calls = [
            ToolCall(id="tc_1", name="file_read", arguments='{"value":"a"}'),
            ToolCall(id="tc_2", name="bash", arguments='{"value":"b"}'),
        ]
        results = [r async for r in executor.run(calls)]

        assert len(results) == 2
        assert all(r.success for r in results)

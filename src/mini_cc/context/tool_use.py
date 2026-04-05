from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from mini_cc.models import ToolCall, ToolResultEvent

GetToolSchemasFn = Callable[[], list[dict[str, Any]]]
ExecuteToolCallsFn = Callable[[list[ToolCall]], AsyncGenerator[ToolResultEvent, None]]
CheckPermissionFn = Callable[[str], bool]
IsInterruptedFn = Callable[[], bool]
TraceFn = Callable[[str, dict[str, Any]], None]


class ToolUseContext:
    def __init__(
        self,
        *,
        get_schemas: GetToolSchemasFn,
        execute: ExecuteToolCallsFn,
        check_permission: CheckPermissionFn | None = None,
        is_interrupted: IsInterruptedFn | None = None,
        on_trace: TraceFn | None = None,
    ) -> None:
        self._get_schemas = get_schemas
        self._execute = execute
        self._check_permission = check_permission or (lambda _: True)
        self._is_interrupted = is_interrupted or (lambda: False)
        self._on_trace = on_trace or (lambda event, kwargs: None)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return self._get_schemas()

    async def execute(self, tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
        async for result in self._execute(tool_calls):
            yield result

    def check_permission(self, tool_name: str) -> bool:
        return self._check_permission(tool_name)

    @property
    def is_interrupted(self) -> bool:
        return self._is_interrupted()

    def trace(self, event: str, **kwargs: Any) -> None:
        self._on_trace(event, kwargs)

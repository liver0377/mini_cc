from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from mini_cc.query_engine.state import ToolCall, ToolResultEvent
from mini_cc.tools.base import BaseTool

_SAFE_TOOL_NAMES = {"file_read", "glob", "grep"}

PreExecuteHook = Callable[[str, dict[str, Any]], None]


class StreamingToolExecutor:
    def __init__(
        self,
        tool_registry: Any,
        pre_execute_hook: PreExecuteHook | None = None,
    ) -> None:
        self._registry = tool_registry
        self._pre_execute_hook = pre_execute_hook

    async def run(self, tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
        safe_tasks: list[tuple[ToolCall, BaseTool, dict[str, Any]]] = []
        unsafe_tasks: list[tuple[ToolCall, BaseTool, dict[str, Any]]] = []

        for tc in tool_calls:
            tool = self._registry.get(tc.name)
            if tool is None:
                yield ToolResultEvent(
                    tool_call_id=tc.id,
                    name=tc.name,
                    output=f"Unknown tool: {tc.name}",
                    success=False,
                )
                continue

            try:
                kwargs = json.loads(tc.arguments)
            except json.JSONDecodeError:
                yield ToolResultEvent(
                    tool_call_id=tc.id,
                    name=tc.name,
                    output="Invalid JSON arguments",
                    success=False,
                )
                continue

            entry = (tc, tool, kwargs)
            if _is_concurrency_safe(tool):
                safe_tasks.append(entry)
            else:
                unsafe_tasks.append(entry)

        for coro in asyncio.as_completed([self._execute_tool(tc, tool, kwargs) for tc, tool, kwargs in safe_tasks]):
            result = await coro
            yield result

        for tc, tool, kwargs in unsafe_tasks:
            result = await self._execute_tool(tc, tool, kwargs)
            yield result

    async def _execute_tool(self, tc: ToolCall, tool: BaseTool, kwargs: dict[str, Any]) -> ToolResultEvent:
        if self._pre_execute_hook is not None:
            self._pre_execute_hook(tool.name, kwargs)
        result = await tool.async_execute(**kwargs)
        return ToolResultEvent(
            tool_call_id=tc.id,
            name=tool.name,
            output=result.output,
            success=result.success,
        )


def _is_concurrency_safe(tool: BaseTool) -> bool:
    return tool.name in _SAFE_TOOL_NAMES

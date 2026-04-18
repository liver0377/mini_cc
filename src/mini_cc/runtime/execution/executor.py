from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from mini_cc.models import ToolCall, ToolResultEvent
from mini_cc.runtime.execution.policy import ExecutionPolicy
from mini_cc.tools import READONLY_TOOL_NAMES
from mini_cc.tools.base import BaseTool

PreExecuteHook = Callable[[str, dict[str, Any]], None]
IsInterruptedFn = Callable[[], bool]
_DEFAULT_TOOL_TIMEOUT = 300.0


class StreamingToolExecutor:
    def __init__(
        self,
        tool_registry: Any,
        pre_execute_hook: PreExecuteHook | None = None,
        is_interrupted: IsInterruptedFn | None = None,
        policy: ExecutionPolicy | None = None,
        tool_timeout: float = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        self._registry = tool_registry
        self._pre_execute_hook = pre_execute_hook
        self._is_interrupted = is_interrupted or (lambda: False)
        self._policy = policy
        self._tool_timeout = tool_timeout

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

            if self._policy is not None:
                allowed, reason = self._policy.validate_tool_call(tool.name, kwargs)
                if not allowed:
                    yield ToolResultEvent(
                        tool_call_id=tc.id,
                        name=tool.name,
                        output=reason,
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
        call_kwargs = dict(kwargs)
        if tool.name == "bash":
            call_kwargs["_is_interrupted"] = self._is_interrupted
        try:
            result = await asyncio.wait_for(tool.async_execute(**call_kwargs), timeout=self._tool_timeout)
        except TimeoutError:
            return ToolResultEvent(
                tool_call_id=tc.id,
                name=tool.name,
                output=f"工具执行超时 ({self._tool_timeout}s)",
                success=False,
            )
        return ToolResultEvent(
            tool_call_id=tc.id,
            name=tool.name,
            output=result.output,
            success=result.success,
        )


def _is_concurrency_safe(tool: BaseTool) -> bool:
    return tool.name in READONLY_TOOL_NAMES

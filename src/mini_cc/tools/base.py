from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from functools import partial
from typing import Any

from pydantic import BaseModel


class ToolResult(BaseModel):
    output: str = ""
    error: str | None = None
    success: bool = True


class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> type[BaseModel]: ...

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult: ...

    async def async_execute(self, **kwargs: Any) -> ToolResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(self.execute, **kwargs))

    def to_api_format(self) -> dict[str, Any]:
        schema = self.input_schema.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def to_api_format(self) -> list[dict[str, Any]]:
        return [tool.to_api_format() for tool in self._tools.values()]

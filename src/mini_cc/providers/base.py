from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Protocol, runtime_checkable

from mini_cc.models import Event, Message


@runtime_checkable
class LLMProvider(Protocol):
    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> AsyncGenerator[Event, None]: ...

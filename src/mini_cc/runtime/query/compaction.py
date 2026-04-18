from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mini_cc.models import CompactOccurred, Message, QueryState

CompactFn = Callable[[list[Message]], Awaitable[str]]
ShouldCompactFn = Callable[[list[Message]], bool]
ReplaceSummaryFn = Callable[[QueryState, str], None]


@dataclass
class CompactionController:
    compact_fn: CompactFn | None = None
    should_compact_fn: ShouldCompactFn | None = None
    replace_summary_fn: ReplaceSummaryFn | None = None

    @property
    def is_configured(self) -> bool:
        return self.compact_fn is not None and self.replace_summary_fn is not None

    def should_compact(self, messages: list[Message]) -> bool:
        if self.should_compact_fn is None:
            return False
        return self.should_compact_fn(messages)

    async def compact(self, state: QueryState, *, reason: str) -> CompactOccurred | None:
        if not self.is_configured:
            return None
        if self.should_compact_fn is not None and not self.should_compact_fn(state.messages):
            return None
        assert self.compact_fn is not None
        assert self.replace_summary_fn is not None
        summary = await self.compact_fn(state.messages)
        self.replace_summary_fn(state, summary)
        return CompactOccurred(reason=reason)

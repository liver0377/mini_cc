from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from mini_cc.memory.prompts import EXTRACTION_SYSTEM_PROMPT
from mini_cc.memory.store import MemoryItem, list_memories, save_memory
from mini_cc.query_engine.state import Message, QueryState, Role, TextDelta

logger = logging.getLogger(__name__)

MIN_NEW_MESSAGES = 4

PostTurnHook = Any


class MemoryExtractor:
    def __init__(self, stream_fn: Any, cwd: str) -> None:
        self._stream_fn = stream_fn
        self._cwd_str = cwd
        self._last_extracted_count: int = 0
        self._bg_tasks: set[asyncio.Task[None]] = set()

    def should_extract(self, state: QueryState) -> bool:
        non_system = [m for m in state.messages if m.role != Role.SYSTEM]
        new_count = len(non_system) - self._last_extracted_count
        return new_count >= MIN_NEW_MESSAGES

    async def extract_memories(self, messages: list[Message]) -> list[MemoryItem]:
        from pathlib import Path

        existing = list_memories(Path(self._cwd_str))
        recent_text = _format_recent_messages(messages)
        prompt_messages = _build_extraction_prompt(existing, recent_text)

        response_text = await _call_llm(self._stream_fn, prompt_messages)

        items = _parse_extraction_response(response_text)
        for item in items:
            save_memory(Path(self._cwd_str), item.name, item.type, item.content, item.description)

        return items

    def fire_and_forget(self, state: QueryState) -> None:
        non_system = [m for m in state.messages if m.role != Role.SYSTEM]
        messages_snapshot = list(non_system)

        async def _bg() -> None:
            try:
                await self.extract_memories(messages_snapshot)
            except Exception:
                logger.debug("Background memory extraction failed", exc_info=True)
            finally:
                non_system_now = [m for m in state.messages if m.role != Role.SYSTEM]
                self._last_extracted_count = len(non_system_now)

        task = asyncio.create_task(_bg())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)


async def _call_llm(
    stream_fn: Any,
    messages: list[Message],
) -> str:
    parts: list[str] = []
    async for event in stream_fn(messages, []):
        if isinstance(event, TextDelta):
            parts.append(event.content)
    return "".join(parts)


def _format_recent_messages(messages: list[Message]) -> str:
    lines: list[str] = []
    for msg in messages:
        if msg.role == Role.SYSTEM:
            continue
        role_label = msg.role.value
        content = msg.content or ""
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"[{role_label}]: {content}")
    return "\n".join(lines)


def _build_extraction_prompt(
    existing_memories: list[Any],
    recent_messages_text: str,
) -> list[Message]:
    user_parts: list[str] = []

    if existing_memories:
        user_parts.append("## 已有记忆\n")
        for mem in existing_memories:
            user_parts.append(f"- {mem.name} (type: {mem.type}): {mem.description}")
        user_parts.append("")

    user_parts.append("## 最近对话\n")
    user_parts.append(recent_messages_text)

    return [
        Message(role=Role.SYSTEM, content=EXTRACTION_SYSTEM_PROMPT),
        Message(role=Role.USER, content="\n".join(user_parts)),
    ]


def _parse_extraction_response(text: str) -> list[MemoryItem]:
    json_str = text.strip()

    match = re.search(r"```json\s*(.*?)\s*```", json_str, re.DOTALL)
    if match:
        json_str = match.group(1)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return []

    memories_raw = data.get("memories", [])
    if not isinstance(memories_raw, list):
        return []

    items: list[MemoryItem] = []
    for entry in memories_raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        mem_type = entry.get("type", "")
        content = entry.get("content", "")
        description = entry.get("description", "")
        if name and mem_type and content:
            items.append(MemoryItem(name=name, type=mem_type, content=content, description=description))
    return items

from __future__ import annotations

import os
from typing import Any

import tiktoken

from mini_cc.features.compression.prompts import COMPRESSION_SYSTEM_PROMPT
from mini_cc.models import Message, MessageSource, QueryState, Role, TextDelta

_AUTO_COMPACT_THRESHOLD = int(os.environ.get("AUTO_COMPACT_THRESHOLD", "80000"))
_FALLBACK_ENCODING = "cl100k_base"

_encoding_cache: dict[str, tiktoken.Encoding] = {}


def _get_encoding(model: str) -> tiktoken.Encoding:
    if model in _encoding_cache:
        return _encoding_cache[model]
    try:
        enc = tiktoken.encoding_for_model(model)
    except (KeyError, ValueError):
        enc = tiktoken.get_encoding(_FALLBACK_ENCODING)
    _encoding_cache[model] = enc
    return enc


def estimate_tokens(messages: list[Message], model: str = "") -> int:
    enc = _get_encoding(model)
    total = 0
    for msg in messages:
        total += 4
        if msg.content:
            total += len(enc.encode(msg.content))
        for tc in msg.tool_calls:
            total += len(enc.encode(tc.arguments))
        if msg.name:
            total += len(enc.encode(msg.name))
    return total


def should_auto_compact(messages: list[Message], model: str = "") -> bool:
    return estimate_tokens(messages, model) >= _AUTO_COMPACT_THRESHOLD


def replace_with_summary(state: QueryState, summary: str) -> None:
    system_msg = state.messages[0] if state.messages and state.messages[0].role == Role.SYSTEM else None
    state.messages.clear()
    if system_msg:
        state.messages.append(system_msg)
    state.messages.append(
        Message(
            role=Role.USER,
            content=f"以下是之前对话的摘要：\n\n{summary}",
            source=MessageSource.INTERNAL,
        )
    )


def _format_messages_for_compression(messages: list[Message]) -> str:
    parts: list[str] = []
    for msg in messages:
        if not _is_compression_relevant(msg):
            continue
        role_label = msg.role.value
        content = msg.content or ""
        if len(content) > 2000:
            content = content[:2000] + "...(已截断)"
        if msg.role == Role.ASSISTANT and msg.tool_calls:
            tc_summary = ", ".join(f"{tc.name}({tc.arguments[:100]})" for tc in msg.tool_calls)
            parts.append(f"[{role_label}]: {content}\n  工具调用: {tc_summary}")
        elif msg.role == Role.TOOL:
            parts.append(f"[tool:{msg.name}]: {content}")
        else:
            parts.append(f"[{role_label}]: {content}")
    return "\n\n".join(parts)


async def _call_llm_for_summary(
    stream_fn: Any,
    messages: list[Message],
) -> str:
    parts: list[str] = []
    async for event in stream_fn(messages, []):
        if isinstance(event, TextDelta):
            parts.append(event.content)
    return "".join(parts)


async def compress_messages(
    messages: list[Message],
    stream_fn: Any,
    model: str = "",
) -> str:
    conversation_text = _format_messages_for_compression(messages)

    existing_summary = ""
    non_system = [m for m in messages if _is_compression_relevant(m)]
    if (
        len(non_system) >= 1
        and non_system[0].role == Role.USER
        and non_system[0].content
        and non_system[0].content.startswith("以下是之前对话的摘要：")
    ):
        existing_summary = non_system[0].content

    if existing_summary:
        user_content = f"## 已有摘要\n{existing_summary}\n\n## 最近对话\n{conversation_text}"
    else:
        user_content = conversation_text

    prompt_messages = [
        Message(role=Role.SYSTEM, content=COMPRESSION_SYSTEM_PROMPT),
        Message(role=Role.USER, content=user_content),
    ]

    return await _call_llm_for_summary(stream_fn, prompt_messages)


def _is_compression_relevant(message: Message) -> bool:
    if message.role == Role.SYSTEM:
        return False
    return message.source in {MessageSource.USER, MessageSource.INTERNAL}

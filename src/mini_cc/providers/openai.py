from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import openai

from mini_cc.query_engine.state import (
    Event,
    Message,
    Role,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)


class OpenAIProvider:
    def __init__(self, model: str, base_url: str, api_key: str) -> None:
        self._model = model
        self._client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> AsyncGenerator[Event, None]:
        api_messages: list[Any] = [_convert_message(msg) for msg in messages]
        api_tools: Any = tools if tools else openai.NOT_GIVEN

        tool_call_buffers: dict[int, dict[str, str]] = {}

        response = self._client.chat.completions.create(
            model=self._model,
            messages=api_messages,
            tools=api_tools,
            stream=True,
        )

        async for chunk in await response:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                yield TextDelta(content=delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index

                    if idx not in tool_call_buffers:
                        tool_call_buffers[idx] = {"id": tc_delta.id or "", "name": "", "arguments": ""}

                    buf = tool_call_buffers[idx]

                    if tc_delta.id:
                        buf["id"] = tc_delta.id

                    if tc_delta.function:
                        if tc_delta.function.name:
                            buf["name"] = tc_delta.function.name
                            yield ToolCallStart(tool_call_id=buf["id"], name=buf["name"])
                        if tc_delta.function.arguments:
                            buf["arguments"] += tc_delta.function.arguments
                            yield ToolCallDelta(
                                tool_call_id=buf["id"],
                                arguments_json_delta=tc_delta.function.arguments,
                            )

                    if choice.finish_reason == "tool_calls":
                        yield ToolCallEnd(tool_call_id=buf["id"])


def _convert_message(msg: Message) -> dict[str, Any]:
    result: dict[str, Any] = {"role": msg.role.value}

    if msg.role == Role.TOOL:
        result["tool_call_id"] = msg.tool_call_id
        result["content"] = msg.content or ""
        return result

    if msg.role == Role.ASSISTANT and msg.tool_calls:
        result["content"] = msg.content
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in msg.tool_calls
        ]
        return result

    if msg.content is not None:
        result["content"] = msg.content

    return result

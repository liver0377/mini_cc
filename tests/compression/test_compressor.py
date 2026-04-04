from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mini_cc.compression.compressor import (
    ContextLengthExceededError,
    compress_messages,
    estimate_tokens,
    replace_with_summary,
    should_auto_compact,
)
from mini_cc.query_engine.state import Message, QueryState, Role, TextDelta


class TestEstimateTokens:
    def test_empty_messages(self) -> None:
        assert estimate_tokens([]) == 0

    def test_single_message(self) -> None:
        msgs = [Message(role=Role.USER, content="Hello world")]
        tokens = estimate_tokens(msgs)
        assert tokens > 0

    def test_system_message_counts(self) -> None:
        msgs = [Message(role=Role.SYSTEM, content="You are a helpful assistant.")]
        tokens = estimate_tokens(msgs)
        assert tokens > 0

    def test_tool_call_arguments_counted(self) -> None:
        from mini_cc.query_engine.state import ToolCall

        msgs = [
            Message(
                role=Role.ASSISTANT,
                content=None,
                tool_calls=[ToolCall(id="1", name="bash", arguments='{"command": "ls"}')],
            )
        ]
        tokens = estimate_tokens(msgs)
        assert tokens > 0

    def test_name_field_counted(self) -> None:
        msgs = [Message(role=Role.TOOL, content="output", tool_call_id="1", name="bash")]
        tokens_with_name = estimate_tokens(msgs)
        msgs_no_name = [Message(role=Role.TOOL, content="output", tool_call_id="1")]
        tokens_no_name = estimate_tokens(msgs_no_name)
        assert tokens_with_name > tokens_no_name

    def test_more_text_more_tokens(self) -> None:
        short = [Message(role=Role.USER, content="hi")]
        long = [Message(role=Role.USER, content="This is a much longer message with many more tokens")]
        assert estimate_tokens(long) > estimate_tokens(short)


class TestShouldAutoCompact:
    def test_below_threshold(self) -> None:
        msgs = [Message(role=Role.USER, content="short")]
        assert not should_auto_compact(msgs)

    def test_at_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("mini_cc.compression.compressor._AUTO_COMPACT_THRESHOLD", 10)
        msgs = [Message(role=Role.USER, content="This is a longer message that should exceed the tiny threshold")]
        assert should_auto_compact(msgs)


class TestReplaceWithSummary:
    def test_preserves_system_message(self) -> None:
        state = QueryState(
            messages=[
                Message(role=Role.SYSTEM, content="system prompt"),
                Message(role=Role.USER, content="hello"),
                Message(role=Role.ASSISTANT, content="hi"),
            ]
        )
        replace_with_summary(state, "Summary of conversation")
        assert len(state.messages) == 2
        assert state.messages[0].role == Role.SYSTEM
        assert state.messages[0].content == "system prompt"
        assert state.messages[1].role == Role.USER
        assert "Summary of conversation" in state.messages[1].content

    def test_works_without_system_message(self) -> None:
        state = QueryState(
            messages=[
                Message(role=Role.USER, content="hello"),
            ]
        )
        replace_with_summary(state, "Summary")
        assert len(state.messages) == 1
        assert state.messages[0].role == Role.USER

    def test_clears_all_non_system(self) -> None:
        state = QueryState(
            messages=[
                Message(role=Role.SYSTEM, content="sys"),
                Message(role=Role.USER, content="u1"),
                Message(role=Role.ASSISTANT, content="a1"),
                Message(role=Role.USER, content="u2"),
                Message(role=Role.ASSISTANT, content="a2"),
            ]
        )
        replace_with_summary(state, "All summarized")
        assert len(state.messages) == 2
        assert state.messages[1].content == "以下是之前对话的摘要：\n\nAll summarized"


class TestCompressMessages:
    async def test_returns_summary(self) -> None:
        async def _fake_stream(messages: object, tools: object):
            yield TextDelta(content="This is the summary of the conversation.")

        msgs = [
            Message(role=Role.SYSTEM, content="system"),
            Message(role=Role.USER, content="Fix the bug"),
            Message(role=Role.ASSISTANT, content="I'll fix it."),
        ]
        result = await compress_messages(msgs, _fake_stream)
        assert "summary" in result.lower()

    async def test_includes_existing_summary(self) -> None:
        received_messages: list[object] = []

        async def _fake_stream(messages: list[Message], tools: object):
            received_messages.extend(messages)
            yield TextDelta(content="Updated summary")

        msgs = [
            Message(role=Role.SYSTEM, content="system"),
            Message(
                role=Role.USER,
                content="以下是之前对话的摘要：\n\nPrevious summary here",
            ),
            Message(role=Role.USER, content="New message"),
        ]
        result = await compress_messages(msgs, _fake_stream)
        assert "Updated summary" in result
        user_content = received_messages[1].content
        assert "已有摘要" in user_content
        assert "最近对话" in user_content


class TestContextLengthExceededError:
    def test_is_exception(self) -> None:
        err = ContextLengthExceededError("too long")
        assert isinstance(err, Exception)
        assert "too long" in str(err)

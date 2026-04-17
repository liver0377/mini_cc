from __future__ import annotations

from mini_cc.models import (
    QueryState,
    QueryTracking,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultEvent,
    collect_tool_calls,
)


class TestCollectToolCalls:
    def test_empty_events(self) -> None:
        assert collect_tool_calls([]) == []

    def test_no_tool_call_events(self) -> None:
        events: list = [
            TextDelta(content="hello"),
            TextDelta(content=" world"),
        ]
        assert collect_tool_calls(events) == []

    def test_single_tool_call(self) -> None:
        events: list = [
            ToolCallStart(tool_call_id="tc_1", name="file_read"),
            ToolCallDelta(tool_call_id="tc_1", arguments_json_delta='{"file'),
            ToolCallDelta(tool_call_id="tc_1", arguments_json_delta='_path": "/tmp/a"}'),
            ToolCallEnd(tool_call_id="tc_1"),
        ]
        result = collect_tool_calls(events)
        assert len(result) == 1
        assert result[0].id == "tc_1"
        assert result[0].name == "file_read"
        assert result[0].arguments == '{"file_path": "/tmp/a"}'

    def test_multiple_tool_calls(self) -> None:
        events: list = [
            ToolCallStart(tool_call_id="tc_1", name="file_read"),
            ToolCallDelta(tool_call_id="tc_1", arguments_json_delta='{"a":1}'),
            ToolCallStart(tool_call_id="tc_2", name="bash"),
            ToolCallDelta(tool_call_id="tc_2", arguments_json_delta='{"cmd":"ls"}'),
            ToolCallEnd(tool_call_id="tc_1"),
            ToolCallEnd(tool_call_id="tc_2"),
        ]
        result = collect_tool_calls(events)
        assert len(result) == 2
        assert result[0].id == "tc_1"
        assert result[0].arguments == '{"a":1}'
        assert result[1].id == "tc_2"
        assert result[1].arguments == '{"cmd":"ls"}'

    def test_interleaved_with_text_events(self) -> None:
        events: list = [
            TextDelta(content="Let me read that file."),
            ToolCallStart(tool_call_id="tc_1", name="grep"),
            ToolCallDelta(tool_call_id="tc_1", arguments_json_delta='{"p":"fn"}'),
            ToolResultEvent(tool_call_id="other", name="old", output="x", success=True),
            ToolCallEnd(tool_call_id="tc_1"),
        ]
        result = collect_tool_calls(events)
        assert len(result) == 1
        assert result[0].name == "grep"
        assert result[0].arguments == '{"p":"fn"}'

    def test_start_without_delta(self) -> None:
        events: list = [
            ToolCallStart(tool_call_id="tc_1", name="glob"),
            ToolCallEnd(tool_call_id="tc_1"),
        ]
        result = collect_tool_calls(events)
        assert len(result) == 1
        assert result[0].arguments == ""

    def test_preserves_order(self) -> None:
        events: list = [
            ToolCallStart(tool_call_id="tc_b", name="bash"),
            ToolCallStart(tool_call_id="tc_a", name="file_read"),
        ]
        result = collect_tool_calls(events)
        assert [tc.id for tc in result] == ["tc_b", "tc_a"]


class TestQueryState:
    def test_defaults(self) -> None:
        state = QueryState()
        assert state.messages == []
        assert state.turn_count == 0

    def test_with_messages(self) -> None:
        state = QueryState(
            messages=[{"role": "user", "content": "hi"}],
            turn_count=3,
        )
        assert state.turn_count == 3


class TestQueryTracking:
    def test_defaults(self) -> None:
        t = QueryTracking()
        assert t.turn == 0

    def test_custom_turn(self) -> None:
        t = QueryTracking(turn=5)
        assert t.turn == 5

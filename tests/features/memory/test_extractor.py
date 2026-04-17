from __future__ import annotations

from mini_cc.features.memory.extractor import (
    MIN_NEW_MESSAGES,
    MemoryExtractor,
    _parse_extraction_response,
)
from mini_cc.models import Message, QueryState, Role


class TestParseExtractionResponse:
    def test_valid_json(self) -> None:
        text = '{"memories": [{"name": "user_role", "type": "user", "content": "test", "description": "desc"}]}'
        items = _parse_extraction_response(text)
        assert len(items) == 1
        assert items[0].name == "user_role"
        assert items[0].type == "user"
        assert items[0].content == "test"

    def test_json_code_block(self) -> None:
        text = '```json\n{"memories": [{"name": "test", "type": "project", "content": "c", "description": "d"}]}\n```'
        items = _parse_extraction_response(text)
        assert len(items) == 1

    def test_empty_memories(self) -> None:
        text = '{"memories": []}'
        items = _parse_extraction_response(text)
        assert items == []

    def test_invalid_json(self) -> None:
        text = "not json at all"
        items = _parse_extraction_response(text)
        assert items == []

    def test_missing_fields_skipped(self) -> None:
        text = '{"memories": [{"name": "test", "type": "user"}]}'
        items = _parse_extraction_response(text)
        assert items == []

    def test_multiple_items(self) -> None:
        text = '{"memories": [{"name": "a", "type": "user", "content": "c1", "description": "d1"}, {"name": "b", "type": "project", "content": "c2", "description": "d2"}]}'
        items = _parse_extraction_response(text)
        assert len(items) == 2

    def test_non_list_memories(self) -> None:
        text = '{"memories": "not a list"}'
        items = _parse_extraction_response(text)
        assert items == []

    def test_non_dict_entry_skipped(self) -> None:
        text = '{"memories": ["not a dict"]}'
        items = _parse_extraction_response(text)
        assert items == []


class TestShouldExtract:
    def _make_state(self, msg_count: int) -> QueryState:
        state = QueryState()
        state.messages.append(Message(role=Role.SYSTEM, content="system"))
        for i in range(msg_count):
            state.messages.append(Message(role=Role.USER, content=f"msg {i}"))
        return state

    def test_below_threshold(self) -> None:
        extractor = MemoryExtractor(stream_fn=None, cwd="/tmp")
        state = self._make_state(MIN_NEW_MESSAGES - 1)
        assert not extractor.should_extract(state)

    def test_at_threshold(self) -> None:
        extractor = MemoryExtractor(stream_fn=None, cwd="/tmp")
        state = self._make_state(MIN_NEW_MESSAGES)
        assert extractor.should_extract(state)

    def test_above_threshold(self) -> None:
        extractor = MemoryExtractor(stream_fn=None, cwd="/tmp")
        state = self._make_state(MIN_NEW_MESSAGES + 5)
        assert extractor.should_extract(state)

    def test_increments_after_extraction(self) -> None:
        extractor = MemoryExtractor(stream_fn=None, cwd="/tmp")
        state = self._make_state(MIN_NEW_MESSAGES)
        assert extractor.should_extract(state)

        extractor._last_extracted_count = MIN_NEW_MESSAGES

        state.messages.append(Message(role=Role.USER, content="one more"))
        assert not extractor.should_extract(state)

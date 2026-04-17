from __future__ import annotations

from mini_cc.app.tui.widgets.input_area import _height_for_text


class TestInputAreaHeight:
    def test_single_line_keeps_min_height(self) -> None:
        assert _height_for_text("hello") == 3

    def test_multiline_expands_height(self) -> None:
        assert _height_for_text("a\nb\nc") == 5

    def test_height_is_capped(self) -> None:
        text = "\n".join(str(i) for i in range(20))
        assert _height_for_text(text) == 8

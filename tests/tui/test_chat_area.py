from __future__ import annotations

from mini_cc.tui.widgets.chat_area import _AGENT_COLORS, ChatArea


class TestChatAreaInit:
    def test_default_state(self):
        ca = ChatArea()
        assert ca._current_stream is None
        assert ca._agent_color_index == {}


class TestAgentColorAssignment:
    def test_first_agent_gets_first_color(self):
        ca = ChatArea()
        color = ca._agent_color("agent1")
        assert color == _AGENT_COLORS[0]

    def test_different_agents_get_different_colors(self):
        ca = ChatArea()
        colors = set()
        for i in range(6):
            colors.add(ca._agent_color(f"agent{i}"))
        assert len(colors) == 6

    def test_color_reuse_after_six(self):
        ca = ChatArea()
        c1 = ca._agent_color("a1")
        for i in range(2, 8):
            ca._agent_color(f"a{i}")
        c7 = ca._agent_color("a7")
        assert c1 == c7

    def test_same_agent_same_color(self):
        ca = ChatArea()
        c1 = ca._agent_color("agent1")
        c2 = ca._agent_color("agent1")
        assert c1 == c2


class TestAgentColors:
    def test_six_colors_available(self):
        assert len(_AGENT_COLORS) == 6

    def test_colors_are_valid(self):
        for color in _AGENT_COLORS:
            assert isinstance(color, str)
            assert len(color) > 0

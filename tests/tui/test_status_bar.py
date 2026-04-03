from __future__ import annotations

from mini_cc.tui.widgets.status_bar import StatusBar


class TestStatusBarInit:
    def test_default_mode(self):
        sb = StatusBar()
        assert sb._mode == "build"
        assert sb._agent_count == 0

    def test_update_info(self):
        sb = StatusBar()
        sb.update_info("plan", "GPT-4o")
        assert sb._mode == "plan"
        assert sb._model == "GPT-4o"

    def test_set_mode(self):
        sb = StatusBar()
        sb.set_mode("plan")
        assert sb._mode == "plan"

    def test_update_agent_count(self):
        sb = StatusBar()
        sb.update_agent_count(3)
        assert sb._agent_count == 3


class TestStatusBarDisplay:
    def test_build_mode_display(self):
        sb = StatusBar()
        sb.update_info("build", "GPT-4o")
        rendered = str(sb.render())
        assert "Build" in rendered

    def test_plan_mode_display(self):
        sb = StatusBar()
        sb.update_info("plan", "GPT-4o")
        rendered = str(sb.render())
        assert "Plan" in rendered

    def test_agent_count_shown_when_nonzero(self):
        sb = StatusBar()
        sb.update_info("build", "GPT-4o")
        sb.update_agent_count(2)
        rendered = str(sb.render())
        assert "子 Agent: 2" in rendered

    def test_agent_count_hidden_when_zero(self):
        sb = StatusBar()
        sb.update_info("build", "GPT-4o")
        sb.update_agent_count(0)
        rendered = str(sb.render())
        assert "子 Agent" not in rendered

    def test_ctrl_a_hint_present(self):
        sb = StatusBar()
        sb.update_info("build", "test-model")
        rendered = str(sb.render())
        assert "Ctrl+A" in rendered


class TestStatusBarSpinner:
    def test_tick_spinner_no_agents(self):
        sb = StatusBar()
        sb.update_agent_count(0)
        initial_idx = sb._spinner_idx
        sb.tick_spinner()
        assert sb._spinner_idx == initial_idx

    def test_tick_spinner_with_agents(self):
        sb = StatusBar()
        sb.update_agent_count(1)
        initial_idx = sb._spinner_idx
        sb.tick_spinner()
        assert sb._spinner_idx == (initial_idx + 1) % 10

    def test_tick_spinner_wraps(self):
        sb = StatusBar()
        sb.update_agent_count(1)
        sb._spinner_idx = 9
        sb.tick_spinner()
        assert sb._spinner_idx == 0

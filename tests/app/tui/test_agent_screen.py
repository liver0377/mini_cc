from __future__ import annotations

import os
from unittest.mock import MagicMock

from mini_cc.models import AgentConfig, AgentStatus
from mini_cc.app.tui.screens.agent_screen import _STATUS_COLORS, _STATUS_ICONS, AgentScreen
from mini_cc.runtime.agents import AgentManager, SubAgent


def _make_sub_agent(
    agent_id: str = "a3f7b2c1",
    task_id: int = 1,
    status: AgentStatus = AgentStatus.CREATED,
) -> MagicMock:
    agent = MagicMock(spec=SubAgent)
    agent.config = AgentConfig(agent_id=agent_id, workspace_path="/tmp/project")
    agent.task_id = task_id
    agent.status = status
    agent.state = MagicMock()
    agent.state.messages = []
    return agent


def _make_manager_with_agents(*agents: MagicMock) -> MagicMock:
    manager = MagicMock(spec=AgentManager)
    agent_dict = {a.config.agent_id: a for a in agents}
    manager.agents = agent_dict
    return manager


class TestAgentScreenInit:
    def test_status_icons_coverage(self):
        for status in AgentStatus:
            assert status in _STATUS_ICONS

    def test_status_colors_coverage(self):
        for status in AgentStatus:
            assert status in _STATUS_COLORS

    def test_screen_creates_with_manager(self):
        manager = _make_manager_with_agents()
        screen = AgentScreen(manager)
        assert screen._manager is manager
        assert screen._agents == []
        assert screen._selected_idx == -1


class TestAgentScreenDataOps:
    def test_agents_populated_from_manager(self):
        agent1 = _make_sub_agent("abc12345", 1, AgentStatus.COMPLETED)
        agent2 = _make_sub_agent("def67890", 2, AgentStatus.RUNNING)
        manager = _make_manager_with_agents(agent1, agent2)
        screen = AgentScreen(manager)

        screen._agents = list(manager.agents.values())
        assert len(screen._agents) == 2

    def test_cursor_up_wraps(self):
        agent1 = _make_sub_agent("abc12345", 1)
        agent2 = _make_sub_agent("def67890", 2)
        manager = _make_manager_with_agents(agent1, agent2)
        screen = AgentScreen(manager)
        screen._agents = list(manager.agents.values())
        screen._selected_idx = 0

        screen.action_cursor_up()

        assert screen._selected_idx == 1

    def test_cursor_down_wraps(self):
        agent1 = _make_sub_agent("abc12345", 1)
        agent2 = _make_sub_agent("def67890", 2)
        manager = _make_manager_with_agents(agent1, agent2)
        screen = AgentScreen(manager)
        screen._agents = list(manager.agents.values())
        screen._selected_idx = 1

        screen.action_cursor_down()

        assert screen._selected_idx == 0

    def test_cursor_no_agents(self):
        manager = _make_manager_with_agents()
        screen = AgentScreen(manager)

        screen.action_cursor_up()
        screen.action_cursor_down()

        assert screen._selected_idx == -1

    def test_cancel_agent_calls_cancel(self):
        agent = _make_sub_agent("abc12345", 1, AgentStatus.RUNNING)
        manager = _make_manager_with_agents(agent)
        screen = AgentScreen(manager)
        screen._agents = [agent]
        screen._selected_idx = 0

        screen.action_cancel_agent()

        agent.cancel.assert_called_once()

    def test_cancel_completed_agent_does_nothing(self):
        agent = _make_sub_agent("abc12345", 1, AgentStatus.COMPLETED)
        manager = _make_manager_with_agents(agent)
        screen = AgentScreen(manager)
        screen._agents = [agent]
        screen._selected_idx = 0

        screen.action_cancel_agent()

        agent.cancel.assert_not_called()

    def test_cancel_no_selection(self):
        manager = _make_manager_with_agents()
        screen = AgentScreen(manager)

        screen.action_cancel_agent()

    def test_read_output_exists(self, tmp_path):
        agent = _make_sub_agent("abc12345", 1, AgentStatus.COMPLETED)
        output_content = "agent_id: abc12345\ntask_id: 1\n---\nHello"
        output_file = tmp_path / ".mini_cc" / "tasks" / "abc12345.output"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(output_content, encoding="utf-8")

        screen = AgentScreen(MagicMock(spec=AgentManager))
        screen._agents = [agent]

        cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            output = screen._read_agent_output(agent)
            assert "Hello" in output
        finally:
            os.chdir(cwd)

    def test_read_output_missing(self):
        agent = _make_sub_agent("abc12345", 1, AgentStatus.COMPLETED)
        screen = AgentScreen(MagicMock(spec=AgentManager))
        output = screen._read_agent_output(agent)
        assert output == ""

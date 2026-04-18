from __future__ import annotations

from unittest.mock import MagicMock

from mini_cc.app.tui.screens.agent_screen import _STATUS_COLORS, _STATUS_ICONS, AgentScreen
from mini_cc.models import AgentStatus
from mini_cc.runtime import AgentView, RuntimeFacade


def _make_agent_view(
    agent_id: str = "a3f7b2c1",
    task_id: int = 1,
    status: AgentStatus = AgentStatus.CREATED,
) -> AgentView:
    return AgentView(
        agent_id=agent_id,
        task_id=task_id,
        status=status,
        workspace_path="/tmp/project",
        is_fork=False,
        parent_agent_id=None,
        scope_paths=["src"],
        base_version_stamp="base:clean:1",
        message_count=0,
        prompt_preview="(无消息)",
        output_path=f"/tmp/project/.mini_cc/tasks/{agent_id}.output",
    )


def _make_runtime(*agents: AgentView) -> MagicMock:
    runtime = MagicMock(spec=RuntimeFacade)
    runtime.list_agents.return_value = list(agents)
    runtime.read_agent_output.return_value = ""
    runtime.has_agent_runtime = True
    return runtime


class TestAgentScreenInit:
    def test_status_icons_coverage(self) -> None:
        for status in AgentStatus:
            assert status in _STATUS_ICONS

    def test_status_colors_coverage(self) -> None:
        for status in AgentStatus:
            assert status in _STATUS_COLORS

    def test_screen_creates_with_runtime(self) -> None:
        runtime = _make_runtime()
        screen = AgentScreen(runtime)
        assert screen._runtime is runtime
        assert screen._agents == []
        assert screen._selected_idx == -1


class TestAgentScreenDataOps:
    def test_agents_populated_from_runtime(self) -> None:
        agent1 = _make_agent_view("abc12345", 1, AgentStatus.COMPLETED)
        agent2 = _make_agent_view("def67890", 2, AgentStatus.RUNNING)
        runtime = _make_runtime(agent1, agent2)
        screen = AgentScreen(runtime)

        screen._refresh_data()

        assert len(screen._agents) == 2

    def test_cursor_up_wraps(self) -> None:
        agent1 = _make_agent_view("abc12345", 1)
        agent2 = _make_agent_view("def67890", 2)
        screen = AgentScreen(_make_runtime(agent1, agent2))
        screen._agents = [agent1, agent2]
        screen._selected_idx = 0

        screen.action_cursor_up()

        assert screen._selected_idx == 1

    def test_cursor_down_wraps(self) -> None:
        agent1 = _make_agent_view("abc12345", 1)
        agent2 = _make_agent_view("def67890", 2)
        screen = AgentScreen(_make_runtime(agent1, agent2))
        screen._agents = [agent1, agent2]
        screen._selected_idx = 1

        screen.action_cursor_down()

        assert screen._selected_idx == 0

    def test_cursor_no_agents(self) -> None:
        screen = AgentScreen(_make_runtime())

        screen.action_cursor_up()
        screen.action_cursor_down()

        assert screen._selected_idx == -1

    def test_cancel_agent_calls_runtime(self) -> None:
        agent = _make_agent_view("abc12345", 1, AgentStatus.RUNNING)
        runtime = _make_runtime(agent)
        screen = AgentScreen(runtime)
        screen._agents = [agent]
        screen._selected_idx = 0

        screen.action_cancel_agent()

        runtime.cancel_agents.assert_called_once_with(["abc12345"])

    def test_cancel_completed_agent_does_nothing(self) -> None:
        agent = _make_agent_view("abc12345", 1, AgentStatus.COMPLETED)
        runtime = _make_runtime(agent)
        screen = AgentScreen(runtime)
        screen._agents = [agent]
        screen._selected_idx = 0

        screen.action_cancel_agent()

        runtime.cancel_agents.assert_not_called()

    def test_cancel_no_selection(self) -> None:
        runtime = _make_runtime()
        screen = AgentScreen(runtime)

        screen.action_cancel_agent()

        runtime.cancel_agents.assert_not_called()

    def test_read_output_delegates_to_runtime(self) -> None:
        agent = _make_agent_view("abc12345", 1, AgentStatus.COMPLETED)
        runtime = _make_runtime(agent)
        runtime.read_agent_output.return_value = "agent_id: abc12345\nHello"
        screen = AgentScreen(runtime)

        output = screen._read_agent_output(agent)

        assert "Hello" in output
        runtime.read_agent_output.assert_called_once_with("abc12345")

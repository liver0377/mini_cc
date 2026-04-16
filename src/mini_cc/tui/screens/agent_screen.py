from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from mini_cc.agent.manager import AgentManager
from mini_cc.agent.sub_agent import SubAgent
from mini_cc.models import AgentStatus
from mini_cc.tui.theme import DEFAULT_THEME

_T = DEFAULT_THEME

_STATUS_ICONS: dict[AgentStatus, str] = {
    AgentStatus.CREATED: "⏳",
    AgentStatus.RUNNING: "▶",
    AgentStatus.BACKGROUND_RUNNING: "⟳",
    AgentStatus.COMPLETED: "✓",
    AgentStatus.CANCELLED: "✗",
}

_STATUS_COLORS: dict[AgentStatus, str] = {
    AgentStatus.CREATED: "#d29922",
    AgentStatus.RUNNING: "#238636",
    AgentStatus.BACKGROUND_RUNNING: "#58a6ff",
    AgentStatus.COMPLETED: "#484f58",
    AgentStatus.CANCELLED: "#da3633",
}


class AgentScreen(Screen[None]):
    DEFAULT_CSS = f"""
    AgentScreen {{
        layout: vertical;
        background: $surface;
    }}
    AgentScreen #agent-header {{
        height: 1;
        width: 1fr;
        padding: 0 2;
        background: {_T.status_bg};
        color: $text;
        content-align: left middle;
    }}
    AgentScreen #agent-list {{
        height: 1fr;
        width: 1fr;
        padding: 1 2;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }}
    AgentScreen #agent-list .agent-row {{
        padding: 1 2;
        margin: 0 0 1 0;
        width: 1fr;
    }}
    AgentScreen #agent-list .agent-row:hover {{
        background: $boost;
    }}
    AgentScreen #agent-list .agent-row.selected {{
        background: {_T.tool_border};
        border-left: tall {_T.spinner};
    }}
    AgentScreen #detail-area {{
        height: auto;
        max-height: 60%;
        width: 1fr;
        padding: 1 2;
        border-top: tall {_T.tool_border};
        background: $boost;
        display: none;
    }}
    AgentScreen #detail-area.visible {{
        display: block;
    }}
    """

    BINDINGS = [
        Binding("escape", "back", "返回聊天", show=True),
        Binding("up", "cursor_up", "上移", show=False),
        Binding("down", "cursor_down", "下移", show=False),
        Binding("enter", "view_detail", "查看详情", show=True),
        Binding("c", "cancel_agent", "取消 Agent", show=True),
        Binding("r", "refresh", "刷新", show=True),
    ]

    def __init__(self, agent_manager: AgentManager) -> None:
        super().__init__()
        self._manager = agent_manager
        self._agents: list[SubAgent] = []
        self._selected_idx: int = -1
        self._detail_visible = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("  子 Agent 管理", id="agent-header")
        yield Vertical(
            Static("", id="agent-list"),
            Static("", id="detail-area"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._refresh()

    def action_cursor_up(self) -> None:
        if not self._agents:
            return
        if self._selected_idx <= 0:
            self._selected_idx = len(self._agents) - 1
        else:
            self._selected_idx -= 1
        self._try_render()

    def action_cursor_down(self) -> None:
        if not self._agents:
            return
        if self._selected_idx >= len(self._agents) - 1:
            self._selected_idx = 0
        else:
            self._selected_idx += 1
        self._try_render()

    def action_view_detail(self) -> None:
        if self._selected_idx < 0 or self._selected_idx >= len(self._agents):
            return
        agent = self._agents[self._selected_idx]
        self._show_detail(agent)

    def action_cancel_agent(self) -> None:
        if self._selected_idx < 0 or self._selected_idx >= len(self._agents):
            return
        agent = self._agents[self._selected_idx]
        if agent.status in (AgentStatus.RUNNING, AgentStatus.BACKGROUND_RUNNING, AgentStatus.CREATED):
            agent.cancel()
            self._refresh_data()
            self._try_render()

    def _refresh_data(self) -> None:
        self._agents = list(self._manager.agents.values())
        if self._selected_idx >= len(self._agents):
            self._selected_idx = len(self._agents) - 1
        if not self._agents:
            self._selected_idx = -1
            self._detail_visible = False

    def _try_render(self) -> None:
        try:
            self._render_list()
            if self._detail_visible and self._selected_idx >= 0:
                self._show_detail(self._agents[self._selected_idx])
            else:
                detail = self.query_one("#detail-area", Static)
                detail.set_class(False, "visible")
        except Exception:
            pass

    def _refresh(self) -> None:
        self._refresh_data()
        self._try_render()

    def _render_list(self) -> None:
        list_widget = self.query_one("#agent-list", Static)
        if not self._agents:
            list_widget.update("[dim]暂无子 Agent[/]")
            return

        lines: list[str] = []
        for i, agent in enumerate(self._agents):
            icon = _STATUS_ICONS.get(agent.status, "?")
            color = _STATUS_COLORS.get(agent.status, "white")
            selected_marker = " [dim]◀[/]" if i == self._selected_idx else ""
            prompt_preview = "(无消息)"
            if agent.state.messages:
                last_msg = agent.state.messages[-1]
                if last_msg.content:
                    prompt_preview = last_msg.content[:60]
            line = (
                f"[{color}]{icon}[/] [bold #58a6ff]{agent.config.agent_id}[/]"
                f"  [dim]Task #{agent.task_id}[/]"
                f"  [{color}]{agent.status.value}[/]"
                f"  [dim]{prompt_preview}[/]"
                f"{selected_marker}"
            )
            lines.append(line)

        list_widget.update("\n".join(lines))

    def _show_detail(self, agent: SubAgent) -> None:
        detail = self.query_one("#detail-area", Static)
        self._detail_visible = True
        detail.set_class(True, "visible")

        output_text = self._read_agent_output(agent)
        config = agent.config

        content_parts: list[str] = [
            f"[bold #58a6ff]Agent {config.agent_id}[/]  [dim]Task #{agent.task_id}[/]",
            f"  状态: {_STATUS_ICONS.get(agent.status, '?')} {agent.status.value}",
            f"  Workspace: {config.workspace_path}",
            f"  Fork: {'是' if config.is_fork else '否'}",
            f"  父 Agent: {config.parent_agent_id or '(无)'}",
            f"  Scope: {', '.join(config.scope_paths) if config.scope_paths else '(未声明)'}",
            f"  Base Version: {config.base_version_stamp or '(无)'}",
            f"  消息数: {len(agent.state.messages)}",
        ]

        if output_text:
            preview = output_text[:500] + ("..." if len(output_text) > 500 else "")
            content_parts.append(f"\n[bold]输出:[/]\n{preview}")

        detail.update("\n".join(content_parts))

    def _read_agent_output(self, agent: SubAgent) -> str:
        output_path = Path(f".mini_cc/tasks/{agent.config.agent_id}.output")
        try:
            return output_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""

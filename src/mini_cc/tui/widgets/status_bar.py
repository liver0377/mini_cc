from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from mini_cc.tui.theme import DEFAULT_THEME

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_T = DEFAULT_THEME


class StatusBar(Horizontal):
    DEFAULT_CSS = f"""
    StatusBar {{
        dock: bottom;
        height: 1;
        width: 1fr;
        background: {_T.status_bg};
        color: $text;
        padding: 0 1;
    }}
    StatusBar .status-sep {{
        color: {_T.status_separator};
        width: 1;
    }}
    StatusBar .status-mode {{
        color: $text;
        width: auto;
    }}
    StatusBar .status-model {{
        color: $text-muted;
        width: auto;
    }}
    StatusBar .status-activity {{
        color: {_T.spinner};
        width: auto;
    }}
    StatusBar .status-hints {{
        color: $text-disabled;
        dock: right;
        width: auto;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._mode = "build"
        self._model = ""
        self._agent_count = 0
        self._main_thinking = False
        self._spinner_idx = 0
        self._run_id = ""
        self._run_phase = ""
        self._run_status = ""
        self._current_step = ""

    def compose(self) -> ComposeResult:
        yield Static("", classes="status-mode", id="sb-mode")
        yield Static("│", classes="status-sep")
        yield Static("", classes="status-model", id="sb-model")
        yield Static("│", classes="status-sep")
        yield Static("", classes="status-activity", id="sb-activity")
        yield Static("│", classes="status-sep")
        yield Static("", classes="status-hints", id="sb-hints")

    def update_info(self, mode: str, model: str) -> None:
        self._mode = mode
        self._model = model
        self._refresh_display()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh_display()

    def update_agent_count(self, count: int) -> None:
        self._agent_count = count
        self._refresh_display()

    def set_main_thinking(self, thinking: bool) -> None:
        self._main_thinking = thinking
        self._refresh_display()

    def update_run(self, run_id: str, status: str, phase: str, step_title: str = "") -> None:
        self._run_id = run_id
        self._run_status = status
        self._run_phase = phase
        self._current_step = step_title
        self._refresh_display()

    def clear_run(self) -> None:
        self._run_id = ""
        self._run_phase = ""
        self._run_status = ""
        self._current_step = ""
        self._refresh_display()

    def tick_spinner(self) -> None:
        if self._agent_count > 0 or self._main_thinking:
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
            self._refresh_display()

    def _build_display_text(self) -> str:
        if self._mode == "plan":
            mode_text = "[bold yellow]● Plan[/]"
        else:
            mode_text = f"[bold {_T.mode_build}]● Build[/]"

        spinner = _SPINNER_FRAMES[self._spinner_idx]
        parts: list[str] = []
        if self._main_thinking:
            parts.append(f"{spinner} 思考中")
        if self._agent_count > 0:
            parts.append(f"{spinner} Agent: {self._agent_count}")
        if self._run_id:
            parts.append(f"Run: {self._run_id[:8]}")
        if self._run_phase:
            parts.append(f"Phase: {self._run_phase}")
        if self._current_step:
            parts.append(f"Step: {self._current_step}")
        activity_text = f" {'  '.join(parts)} " if parts else " "

        return (
            f" {mode_text}  │  {self._model or 'unknown'}{activity_text}"
            "  │  Esc 中断 │ Tab 模式 │ Ctrl+A Agent │ Ctrl+R Runs "
        )

    def _refresh_display(self) -> None:
        if not self.is_mounted:
            return

        mode_w = self.query_one("#sb-mode", Static)
        model_w = self.query_one("#sb-model", Static)
        activity_w = self.query_one("#sb-activity", Static)
        hints_w = self.query_one("#sb-hints", Static)

        if self._mode == "plan":
            mode_w.update("[bold yellow]● Plan[/]")
        else:
            mode_w.update(f"[bold {_T.mode_build}]● Build[/]")

        model_w.update(f" {self._model or 'unknown'} ")

        spinner = _SPINNER_FRAMES[self._spinner_idx]
        parts: list[str] = []
        if self._main_thinking:
            parts.append(f"{spinner} 思考中")
        if self._agent_count > 0:
            parts.append(f"{spinner} Agent: {self._agent_count}")
        if self._run_id:
            parts.append(f"Run: {self._run_id[:8]}")
        if self._run_phase:
            parts.append(f"Phase: {self._run_phase}")
        if self._current_step:
            parts.append(f"Step: {self._current_step}")
        activity_w.update(f" {'  '.join(parts)} " if parts else " ")

        hints_w.update(" Esc 中断 │ Tab 模式 │ Ctrl+A Agent │ Ctrl+R Runs ")

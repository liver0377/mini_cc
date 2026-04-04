from __future__ import annotations

from textual.widgets import Static

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        width: 1fr;
        background: $primary;
        color: $text;
        padding: 0 1;
        content-align: left middle;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._mode = "build"
        self._model = ""
        self._agent_count = 0
        self._main_thinking = False
        self._spinner_idx = 0

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

    def tick_spinner(self) -> None:
        if self._agent_count > 0 or self._main_thinking:
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
            self._refresh_display()

    def _refresh_display(self) -> None:
        mode_label = "[bold yellow]Plan[/] (只读)" if self._mode == "plan" else "[bold green]Build[/] (读写)"
        model_label = self._model or "unknown"
        spinner = _SPINNER_FRAMES[self._spinner_idx]
        parts: list[str] = []
        if self._main_thinking:
            parts.append(f"{spinner} 思考中")
        if self._agent_count > 0:
            parts.append(f"{spinner} 子 Agent: {self._agent_count}")
        status_part = f"  │  {'  '.join(parts)}" if parts else ""
        self.update(
            f" 模式: {mode_label}  │  模型: {model_label}{status_part}"
            f"  │  Tab 切换模式  │  Esc 中断  │  Ctrl+A Agent管理"
        )

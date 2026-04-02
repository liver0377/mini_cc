from __future__ import annotations

from textual.widgets import Static


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

    def update_info(self, mode: str, model: str) -> None:
        self._mode = mode
        self._model = model
        self._refresh_display()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh_display()

    def _refresh_display(self) -> None:
        mode_label = "[bold yellow]Plan[/] (只读)" if self._mode == "plan" else "[bold green]Build[/] (读写)"
        model_label = self._model or "unknown"
        self.update(f" 模式: {mode_label}  │  模型: {model_label}  │  Tab 切换模式  │  Esc 中断")

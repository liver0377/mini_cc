from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from mini_cc.repl import EngineContext
from mini_cc.tui.screens.chat_screen import ChatScreen


class MiniCCApp(App[None]):
    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", show=False, priority=True),
        Binding("ctrl+q", "quit", "退出", show=False, priority=True),
    ]

    TITLE = "mini-cc"

    CSS = """
    $user-bg: #1a1a2e;
    $tool-bg: #0d1117;
    $accent: #7c3aed;
    $success: #238636;
    $fail: #da3633;
    $status-bg: #161b22;
    $input-border: #30363d;
    $input-focus: #58a6ff;

    Screen {
        layout: vertical;
        background: $surface;
    }
    """

    def __init__(self, engine_ctx: EngineContext) -> None:
        super().__init__()
        self._engine_ctx = engine_ctx

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self._engine_ctx))

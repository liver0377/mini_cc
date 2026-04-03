from __future__ import annotations

from textual.app import App

from mini_cc.repl import EngineContext
from mini_cc.tui.screens.chat_screen import ChatScreen


class MiniCCApp(App[None]):
    EXIT_ON_CTRL_C = False
    TITLE = "mini-cc"
    CSS = """
    Screen {
        layout: vertical;
    }
    """

    def __init__(self, engine_ctx: EngineContext) -> None:
        super().__init__()
        self._engine_ctx = engine_ctx

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self._engine_ctx))

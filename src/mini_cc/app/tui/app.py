from __future__ import annotations

import asyncio

from textual.app import App
from textual.binding import Binding

from mini_cc.app.tui.screens.chat_screen import ChatScreen
from mini_cc.context.engine_context import EngineContext


class MiniCCApp(App[None]):
    BINDINGS = [
        Binding("ctrl+d", "request_exit", "退出", show=False, priority=True),
        Binding("ctrl+q", "request_exit", "退出", show=False, priority=True),
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
        self._exiting = False

    def on_mount(self) -> None:
        self.push_screen(ChatScreen(self._engine_ctx))

    def action_request_exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        asyncio.create_task(self._do_exit())

    async def _do_exit(self) -> None:
        screen = self.screen
        if isinstance(screen, ChatScreen):
            await screen.graceful_shutdown()
        self.exit()

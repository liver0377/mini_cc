from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from mini_cc.query_engine.state import (
    Event,
    Message,
    QueryState,
    Role,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.repl import EngineContext
from mini_cc.tui.widgets import ChatArea, InputArea, StatusBar
from mini_cc.tui.widgets.input_area import InputArea as InputAreaType

PLAN_MODE = "plan"
BUILD_MODE = "build"


class ChatScreen(Screen[None]):
    DEFAULT_CSS = """
    ChatScreen {
        layout: vertical;
    }
    """

    BINDINGS = [
        Binding("tab", "toggle_mode", "Toggle Plan/Build", show=False),
        Binding("ctrl+c", "app.quit", "Quit", show=False),
        Binding("escape", "interrupt", "Interrupt", show=False),
    ]

    def __init__(self, engine_ctx: EngineContext) -> None:
        super().__init__()
        self._engine_ctx = engine_ctx
        self._state = QueryState(
            messages=[Message(role=Role.SYSTEM, content=engine_ctx.prompt_builder.build(engine_ctx.env_info))]
        )
        self._mode = BUILD_MODE
        self._processing = False
        self._interrupt_event = asyncio.Event()
        self._stream_task: asyncio.Task[None] | None = None
        self._pending_text: str = ""

    def compose(self) -> ComposeResult:
        yield ChatArea()
        yield InputArea()
        yield StatusBar()

    def on_mount(self) -> None:
        status = self.query_one(StatusBar)
        status.update_info(self._mode, self._engine_ctx.env_info.model_name)
        self.query_one(InputAreaType).focus()

    def on_input_area_submitted(self, event: InputArea.Submitted) -> None:
        event.stop()
        if self._processing:
            return
        self._send_message(event.text)

    def action_toggle_mode(self) -> None:
        self._mode = PLAN_MODE if self._mode == BUILD_MODE else BUILD_MODE
        self._rebuild_system_message()
        status = self.query_one(StatusBar)
        status.set_mode(self._mode)

    def action_interrupt(self) -> None:
        if self._processing:
            self._interrupt_event.set()

    def _rebuild_system_message(self) -> None:
        content = self._engine_ctx.prompt_builder.build(self._engine_ctx.env_info, mode=self._mode)
        if self._state.messages and self._state.messages[0].role == Role.SYSTEM:
            self._state.messages[0] = Message(role=Role.SYSTEM, content=content)
        else:
            self._state.messages.insert(0, Message(role=Role.SYSTEM, content=content))

    def _send_message(self, text: str) -> None:
        if text.lower() in {"exit", "quit"}:
            self.app.exit()
            return

        self._pending_text = text
        self._processing = True
        self._interrupt_event.clear()
        self._stream_task = asyncio.create_task(self._run_stream(text))

    async def _run_stream(self, user_text: str) -> None:
        chat = self.query_one(ChatArea)
        input_area = self.query_one(InputAreaType)
        input_area.disabled = True

        try:
            await chat.add_user_message(user_text)
            await chat.begin_assistant_message()
            engine = self._engine_ctx.engine

            async for event in engine.submit_message(user_text, self._state):
                if self._interrupt_event.is_set():
                    await chat.end_assistant_message()
                    await chat.add_system_message("[dim]（已中断）[/]")
                    break
                await self._handle_event(event, chat)
            else:
                await chat.end_assistant_message()

        except Exception as e:
            await chat.end_assistant_message()
            await chat.add_system_message(f"[bold red]错误: {e}[/]")
        finally:
            self._processing = False
            input_area.disabled = False
            input_area.focus()
            self._stream_task = None

    async def _handle_event(self, event: Event, chat: ChatArea) -> None:
        if isinstance(event, TextDelta):
            await chat.append_assistant_text(event.content)
        elif isinstance(event, ToolCallStart):
            await chat.end_assistant_message()
            await chat.add_tool_call(event.name)
        elif isinstance(event, ToolResultEvent):
            await chat.add_tool_result(event.name, event.output, event.success)
            await chat.begin_assistant_message()

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from mini_cc.query_engine.state import (
    AgentCompletionNotificationEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    Message,
    QueryState,
    Role,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.repl import EngineContext
from mini_cc.tui.screens.agent_screen import AgentScreen
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
        Binding("ctrl+a", "open_agent_screen", "Agent 管理", show=True),
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
        self._queued_text: str | None = None
        self._spinner_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        yield ChatArea()
        yield InputArea()
        yield StatusBar()

    def on_mount(self) -> None:
        status = self.query_one(StatusBar)
        status.update_info(self._mode, self._engine_ctx.env_info.model_name)
        self.query_one(InputAreaType).focus()
        self._spinner_task = asyncio.create_task(self._spinner_loop())

    def on_unmount(self) -> None:
        if self._spinner_task is not None:
            self._spinner_task.cancel()
            self._spinner_task = None

    async def _spinner_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.15)
                status = self.query_one(StatusBar)
                status.tick_spinner()
        except asyncio.CancelledError:
            pass

    def on_input_area_submitted(self, event: InputArea.Submitted) -> None:
        event.stop()
        if self._processing:
            self._queued_text = event.text
            return
        self._send_message(event.text)

    def action_toggle_mode(self) -> None:
        self._mode = PLAN_MODE if self._mode == BUILD_MODE else BUILD_MODE
        self._engine_ctx.mode = self._mode
        self._rebuild_system_message()
        status = self.query_one(StatusBar)
        status.set_mode(self._mode)

    def action_interrupt(self) -> None:
        if self._processing:
            self._interrupt_event.set()

    def action_open_agent_screen(self) -> None:
        if self._engine_ctx.agent_manager is not None:
            self.app.push_screen(AgentScreen(self._engine_ctx.agent_manager))

    def _rebuild_system_message(self) -> None:
        content = self._engine_ctx.prompt_builder.build(self._engine_ctx.env_info, mode=self._mode)
        if self._state.messages and self._state.messages[0].role == Role.SYSTEM:
            self._state.messages[0] = Message(role=Role.SYSTEM, content=content)
        else:
            self._state.messages.insert(0, Message(role=Role.SYSTEM, content=content))

    def _send_message(self, text: str) -> None:
        if text.lower() in {"/exit", "/quit"}:
            self.app.exit()
            return

        self._pending_text = text
        self._processing = True
        self._interrupt_event.clear()
        self._stream_task = asyncio.create_task(self._run_stream(text))

    async def _run_stream(self, user_text: str) -> None:
        chat = self.query_one(ChatArea)

        try:
            await chat.add_user_message(user_text)
            await chat.begin_assistant_message()
            engine = self._engine_ctx.engine

            interrupted = False
            async for event in engine.submit_message(user_text, self._state):
                if self._interrupt_event.is_set():
                    await chat.end_assistant_message()
                    await chat.add_system_message("[dim]（已中断）[/]")
                    interrupted = True
                    break
                await self._handle_event(event, chat)
            else:
                await chat.end_assistant_message()

            if not interrupted:
                self._interrupt_event.clear()
                await self._poll_remaining_completions(chat)

        except Exception as e:
            await chat.end_assistant_message()
            await chat.add_system_message(f"[bold red]错误: {e}[/]")
        finally:
            self._processing = False
            self._stream_task = None
            queued = self._queued_text
            if queued is not None:
                self._queued_text = None
                self._send_message(queued)
            else:
                self.query_one(InputAreaType).focus()

    async def _handle_event(self, event: Event, chat: ChatArea) -> None:
        if isinstance(event, TextDelta):
            await chat.append_assistant_text(event.content)
        elif isinstance(event, ToolCallStart):
            await chat.end_assistant_message()
            await chat.add_tool_call(event.name)
        elif isinstance(event, ToolResultEvent):
            await chat.add_tool_result(event.name, event.output, event.success)
            await chat.begin_assistant_message()
            self._refresh_agent_count()
        elif isinstance(event, AgentStartEvent):
            await chat.add_agent_start(event.agent_id, event.task_id, event.prompt)
            self._refresh_agent_count()
        elif isinstance(event, AgentToolCallEvent):
            await chat.add_agent_tool_call(event.agent_id, event.tool_name)
        elif isinstance(event, AgentToolResultEvent):
            await chat.add_agent_tool_result(event.agent_id, event.tool_name, event.success, event.output_preview)
        elif isinstance(event, AgentCompletionNotificationEvent):
            await chat.end_assistant_message()
            await chat.add_agent_notification(
                agent_id=event.agent_id,
                task_id=event.task_id,
                success=event.success,
                output=event.output,
            )
            await chat.begin_assistant_message()
            self._refresh_agent_count()

    async def _poll_remaining_completions(self, chat: ChatArea) -> None:
        completion_queue = self._engine_ctx.completion_queue
        if completion_queue is None:
            return
        remaining = self._engine_ctx.active_agent_count
        if remaining == 0:
            return

        await chat.add_system_message(f"[dim]等待 {remaining} 个后台子 Agent 完成... (Esc 跳过等待)[/]")

        try:
            while self._engine_ctx.active_agent_count > 0:
                if self._interrupt_event.is_set():
                    break
                try:
                    evt = await asyncio.wait_for(completion_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                notification = AgentCompletionNotificationEvent(
                    agent_id=evt.agent_id,
                    task_id=evt.task_id,
                    success=evt.success,
                    output=evt.output,
                    output_path=str(evt.output_path),
                )
                await chat.add_agent_notification(
                    agent_id=notification.agent_id,
                    task_id=notification.task_id,
                    success=notification.success,
                    output=notification.output,
                )
                self._refresh_agent_count()
        finally:
            self._interrupt_event.clear()

    def _refresh_agent_count(self) -> None:
        count = self._engine_ctx.active_agent_count
        self.query_one(StatusBar).update_agent_count(count)

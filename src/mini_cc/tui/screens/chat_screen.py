from __future__ import annotations

import asyncio
import contextlib

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from mini_cc.compression.compressor import compress_messages, replace_with_summary
from mini_cc.context.engine_context import EngineContext
from mini_cc.query_engine.state import (
    AgentCompletionNotificationEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    CompactOccurred,
    Event,
    Message,
    QueryState,
    Role,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.task.models import AgentCompletionEvent
from mini_cc.tui.screens.agent_screen import AgentScreen
from mini_cc.tui.widgets import ChatArea, InputArea, StatusBar
from mini_cc.tui.widgets.completion_popup import CompletionPopup
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
        self._spinner_event: asyncio.Event = asyncio.Event()
        self._active_spinner = False

    def compose(self) -> ComposeResult:
        yield ChatArea()
        yield CompletionPopup()
        yield InputArea()
        yield StatusBar()

    def on_mount(self) -> None:
        status = self.query_one(StatusBar)
        status.update_info(self._mode, self._engine_ctx.env_info.model_name)
        popup = self.query_one(CompletionPopup)
        popup.display = False
        self.query_one(InputAreaType).focus()
        self._spinner_task = asyncio.create_task(self._spinner_loop())

    def on_unmount(self) -> None:
        if self._spinner_task is not None:
            self._spinner_task.cancel()
            self._spinner_task = None

    def _notify_spinner(self) -> None:
        self._active_spinner = self._processing or self._engine_ctx.active_agent_count > 0
        self._spinner_event.set()

    async def _spinner_loop(self) -> None:
        try:
            while True:
                if self._active_spinner:
                    status = self.query_one(StatusBar)
                    status.tick_spinner()
                    await asyncio.sleep(0.15)
                else:
                    self._spinner_event.clear()
                    await self._spinner_event.wait()
        except asyncio.CancelledError:
            pass

    async def graceful_shutdown(self) -> None:
        if self._stream_task is not None and not self._stream_task.done():
            self._interrupt_event.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._stream_task, timeout=2.0)
            self._stream_task = None

        agent_manager = self._engine_ctx.agent_manager
        if agent_manager is not None:
            agent_ids = list(agent_manager.agents.keys())
            for agent_id in agent_ids:
                try:
                    await asyncio.wait_for(agent_manager.cleanup(agent_id), timeout=2.0)
                except TimeoutError:
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
        if text.lower() in {"/exit", "/exit ", "/quit", "/quit "}:
            self.app.exit()
            return

        if text.lower() == "/help":
            asyncio.create_task(self._show_help())
            return

        if text.lower() == "/clear":
            asyncio.create_task(self._clear_chat())
            return

        if text.lower() == "/mode":
            self.action_toggle_mode()
            return

        if text.lower() == "/agents":
            self.action_open_agent_screen()
            return

        if text.strip().lower() == "/compact":
            self._pending_text = text
            self._processing = True
            self._interrupt_event.clear()
            self._notify_spinner()
            self._stream_task = asyncio.create_task(self._run_compact())
            return

        self._pending_text = text
        self._processing = True
        self._interrupt_event.clear()
        self._notify_spinner()
        self._stream_task = asyncio.create_task(self._run_stream(text))

    async def _show_help(self) -> None:
        chat = self.query_one(ChatArea)
        help_text = (
            "[bold]可用命令:[/]\n"
            "  [cyan]/help[/]    显示帮助信息\n"
            "  [cyan]/compact[/] 压缩对话上下文\n"
            "  [cyan]/clear[/]   清空聊天记录\n"
            "  [cyan]/mode[/]    切换 Plan/Build 模式\n"
            "  [cyan]/agents[/]  管理子 Agent\n"
            "  [cyan]/exit[/]    退出程序\n\n"
            "[bold]快捷键:[/]\n"
            "  [dim]Tab[/]      切换 Plan/Build 模式\n"
            "  [dim]Ctrl+A[/]   打开 Agent 管理界面\n"
            "  [dim]Esc[/]      中断当前操作\n"
            "  [dim]Ctrl+P[/]   打开命令面板\n"
            "  [dim]Shift+Enter[/] 换行\n\n"
            "[bold]补全:[/]\n"
            "  [dim]/[/]        输入 / 触发命令补全\n"
            "  [dim]@[/]        输入 @ 触发文件路径补全\n"
        )
        await chat.add_system_message(help_text)
        self.query_one(InputAreaType).focus()

    async def _clear_chat(self) -> None:
        chat = self.query_one(ChatArea)
        await chat.clear_messages()
        await chat.add_system_message("[dim]聊天记录已清空[/]")
        self._state = QueryState(
            messages=[
                Message(role=Role.SYSTEM, content=self._engine_ctx.prompt_builder.build(self._engine_ctx.env_info))
            ]
        )
        self.query_one(InputAreaType).focus()

    async def _run_compact(self) -> None:
        chat = self.query_one(ChatArea)
        status = self.query_one(StatusBar)

        try:
            status.set_main_thinking(True)
            self._notify_spinner()
            summary = await compress_messages(
                self._state.messages,
                self._engine_ctx.engine._stream_fn,
                self._engine_ctx.model,
            )
            replace_with_summary(self._state, summary)
            await chat.add_system_message("[dim]（对话已手动压缩）[/]")
        except Exception as e:
            await chat.add_system_message(f"[bold red]压缩失败: {e}[/]")
        finally:
            status.set_main_thinking(False)
            self._processing = False
            self._stream_task = None
            self._notify_spinner()
            self.query_one(InputAreaType).focus()

    async def _run_stream(self, user_text: str) -> None:
        chat = self.query_one(ChatArea)
        status = self.query_one(StatusBar)

        try:
            status.set_main_thinking(True)
            self._notify_spinner()
            await chat.add_user_message(user_text)
            await chat.begin_assistant_message()

            interrupted = False
            async for event in self._engine_ctx.engine.submit_message(user_text, self._state):
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
                completion_results = await self._poll_remaining_completions(chat)

                if completion_results:
                    await self._submit_agent_results(completion_results, chat, status)
                else:
                    await chat.add_done_marker()

        except Exception as e:
            await chat.end_assistant_message()
            await chat.add_system_message(f"[bold red]错误: {e}[/]")
        finally:
            status.set_main_thinking(False)
            self._processing = False
            self._stream_task = None
            self._notify_spinner()
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
        elif isinstance(event, CompactOccurred):
            label = {
                "auto": "上下文已自动压缩",
                "reactive": "上下文超出限制，已自动压缩后重试",
            }.get(event.reason, "对话已压缩")
            await chat.end_assistant_message()
            await chat.add_system_message(f"[dim]（{label}）[/]")
            await chat.begin_assistant_message()

    async def _poll_remaining_completions(self, chat: ChatArea) -> list[AgentCompletionEvent]:
        completion_queue = self._engine_ctx.completion_queue
        if completion_queue is None:
            return []
        remaining = self._engine_ctx.active_agent_count
        if remaining == 0:
            return []

        await chat.add_system_message(f"[dim]等待 {remaining} 个后台子 Agent 完成... (Esc 跳过等待)[/]")
        self._notify_spinner()

        results: list[AgentCompletionEvent] = []
        try:
            while self._engine_ctx.active_agent_count > 0:
                if self._interrupt_event.is_set():
                    break
                try:
                    evt = await asyncio.wait_for(completion_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                results.append(evt)
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
        return results

    async def _submit_agent_results(
        self,
        results: list[AgentCompletionEvent],
        chat: ChatArea,
        status: StatusBar,
    ) -> None:
        status.set_main_thinking(True)
        self._notify_spinner()
        summary_parts: list[str] = []
        for r in results:
            status_label = "成功" if r.success else "失败"
            summary_parts.append(f"## 子 Agent {r.agent_id} (Task #{r.task_id}) - {status_label}\n\n{r.output}")
        summary = "\n\n---\n\n".join(summary_parts)

        await chat.add_system_message("[dim]子 Agent 全部完成，正在汇总结果...[/]")

        result_prompt = (
            f"以下是之前启动的后台只读子 Agent 的完成结果。\n请基于这些结果，继续回复用户的原始问题。\n\n{summary}"
        )

        await chat.begin_assistant_message()
        try:
            async for event in self._engine_ctx.engine.submit_message(result_prompt, self._state):
                if self._interrupt_event.is_set():
                    await chat.end_assistant_message()
                    await chat.add_system_message("[dim]（已中断）[/]")
                    return
                await self._handle_event(event, chat)
            await chat.end_assistant_message()
        except Exception:
            await chat.end_assistant_message()

        self._refresh_agent_count()
        await chat.add_done_marker()

    def _refresh_agent_count(self) -> None:
        count = self._engine_ctx.active_agent_count
        self.query_one(StatusBar).update_agent_count(count)
        self._notify_spinner()

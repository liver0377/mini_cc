from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from mini_cc.app.presentation import COMPACT_LABELS
from mini_cc.app.tui.screens.agent_screen import AgentScreen
from mini_cc.app.tui.screens.run_screen import RunScreen
from mini_cc.app.tui.widgets import ChatArea, InputArea, StatusBar
from mini_cc.app.tui.widgets.completion_popup import CompletionPopup
from mini_cc.app.tui.widgets.input_area import InputArea as InputAreaType
from mini_cc.context.engine_context import EngineContext
from mini_cc.harness.bootstrap import prepare_run_request
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.models import RunState, Step, StepKind
from mini_cc.harness.runner import RunHarness
from mini_cc.models import (
    AgentCompletionEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    CompactOccurred,
    Event,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.runtime.facade import RuntimeFacade

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
        Binding("ctrl+r", "open_run_screen", "Run 时间线", show=True),
        Binding("escape", "interrupt", "Interrupt", show=False),
    ]

    def __init__(self, engine_ctx: EngineContext) -> None:
        super().__init__()
        self._runtime = RuntimeFacade(engine_ctx)
        self._state = self._runtime.new_query_state()
        self._harness = RunHarness.create_default(
            runtime=self._runtime,
            event_sink=self._on_harness_event,
            query_event_sink=self._on_query_event,
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
        self._current_run: RunState | None = None
        self._assistant_stream_open = False
        self._current_text_step_id: str | None = None
        self._current_step_had_output = False
        self._last_announced_step_key: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        yield ChatArea()
        yield CompletionPopup()
        yield InputArea()
        yield StatusBar()

    def on_mount(self) -> None:
        status = self.query_one(StatusBar)
        status.update_info(self._mode, self._runtime.model_name)
        popup = self.query_one(CompletionPopup)
        popup.display = False
        self.query_one(InputAreaType).focus()
        self._spinner_task = asyncio.create_task(self._spinner_loop())

    def on_unmount(self) -> None:
        if self._spinner_task is not None:
            self._spinner_task.cancel()
            self._spinner_task = None

    def _notify_spinner(self) -> None:
        self._active_spinner = self._processing or self._runtime.active_agent_count > 0
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

        try:
            await asyncio.wait_for(self._runtime.cleanup_agents(), timeout=2.0)
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
        self._runtime.mode = self._mode
        self._rebuild_system_message()
        status = self.query_one(StatusBar)
        status.set_mode(self._mode)

    def action_interrupt(self) -> None:
        if self._processing:
            self._interrupt_event.set()
            if self._stream_task is not None:
                self._stream_task.cancel()
            if self._current_run is not None:
                self._harness.cancel(self._current_run.run_id)

    def action_open_agent_screen(self) -> None:
        if self._runtime.has_agent_runtime:
            self.app.push_screen(AgentScreen(self._runtime))

    def action_open_run_screen(self) -> None:
        self.app.push_screen(
            RunScreen(
                self._harness.store,
                self._harness,
                on_run_selected=self._attach_run_context,
            )
        )

    def _rebuild_system_message(self) -> None:
        self._runtime.apply_system_prompt(self._state, mode=self._mode)

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

        if text.lower() == "/runs":
            self.action_open_run_screen()
            return

        if text.lower() == "/resume":
            self._pending_text = text
            self._processing = True
            self._interrupt_event.clear()
            self._notify_spinner()
            self._stream_task = asyncio.create_task(self._resume_latest_run())
            return

        if text.lower() == "/cancel":
            asyncio.create_task(self._cancel_current_run())
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
        self._stream_task = asyncio.create_task(self._run_goal(text))

    async def _show_help(self) -> None:
        chat = self.query_one(ChatArea)
        help_text = (
            "[bold]可用命令:[/]\n"
            "  [cyan]/help[/]    显示帮助信息\n"
            "  [cyan]/compact[/] 压缩对话上下文\n"
            "  [cyan]/clear[/]   清空聊天记录\n"
            "  [cyan]/mode[/]    切换 Plan/Build 模式\n"
            "  [cyan]/resume[/]  恢复最近一次 Run\n"
            "  [cyan]/cancel[/]  取消当前 Run\n"
            "  [cyan]/runs[/]    打开 Run 时间线面板\n"
            "  [cyan]/agents[/]  管理子 Agent\n"
            "  [cyan]/exit[/]    退出程序\n\n"
            "[bold]快捷键:[/]\n"
            "  [dim]Tab[/]      切换 Plan/Build 模式\n"
            "  [dim]Ctrl+A[/]   打开 Agent 管理界面\n"
            "  [dim]Ctrl+R[/]   打开 Run 时间线面板\n"
            "  [dim]Esc[/]      中断当前操作\n"
            "  [dim]Ctrl+P[/]   打开命令面板\n"
            "  [dim]Ctrl+Enter[/] / [dim]Shift+Enter[/] 换行\n\n"
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
        self._state = self._runtime.new_query_state()
        self._current_run = None
        self._runtime.current_run_id = None
        self._current_text_step_id = None
        self._current_step_had_output = False
        self._last_announced_step_key = None
        self.query_one(StatusBar).clear_run()
        self.query_one(InputAreaType).focus()

    async def _run_compact(self) -> None:
        chat = self.query_one(ChatArea)
        status = self.query_one(StatusBar)

        try:
            status.set_main_thinking(True)
            self._notify_spinner()
            await self._runtime.compact_state(self._state)
            await chat.add_system_message("[dim]（对话已手动压缩）[/]")
        except Exception as e:
            await chat.add_system_message(f"[bold red]压缩失败: {escape(str(e))}[/]")
        finally:
            status.set_main_thinking(False)
            self._processing = False
            self._stream_task = None
            self._notify_spinner()
            self.query_one(InputAreaType).focus()

    async def _run_goal(self, user_text: str) -> None:
        chat = self.query_one(ChatArea)
        status = self.query_one(StatusBar)

        try:
            self._refresh_engine_context()
            status.set_main_thinking(True)
            self._notify_spinner()
            await chat.add_user_message(user_text)
            steps, metadata = prepare_run_request(
                user_text,
                self._mode,
                Path(self._runtime.working_directory),
            )
            self._current_run = await self._harness.run(
                user_text,
                steps=steps,
                metadata={"mode": self._mode, **metadata},
            )
            self._runtime.current_run_id = self._current_run.run_id
            if self._current_run.latest_query_state is not None:
                self._state = self._current_run.latest_query_state
            if self._assistant_stream_open:
                await chat.end_assistant_message()
                self._assistant_stream_open = False
            self._interrupt_event.clear()
            await chat.add_done_marker()

        except asyncio.CancelledError:
            if self._assistant_stream_open:
                await chat.end_assistant_message()
                self._assistant_stream_open = False
            await chat.add_system_message("[dim]（Run 已取消）[/]")
        except Exception as e:
            if self._assistant_stream_open:
                await chat.end_assistant_message()
                self._assistant_stream_open = False
            await chat.add_system_message(f"[bold red]错误: {escape(str(e))}[/]")
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

    async def _resume_latest_run(self) -> None:
        chat = self.query_one(ChatArea)
        status = self.query_one(StatusBar)

        try:
            self._refresh_engine_context()
            status.set_main_thinking(True)
            self._notify_spinner()
            latest_run_id = self._harness.latest_run_id()
            if latest_run_id is None:
                await chat.add_system_message("[dim]没有可恢复的 Run[/]")
                return
            await chat.add_system_message(f"[dim]恢复 Run {latest_run_id[:8]}[/]")
            self._current_run = await self._harness.resume(latest_run_id)
            self._runtime.current_run_id = self._current_run.run_id
            if self._current_run.latest_query_state is not None:
                self._state = self._current_run.latest_query_state
            if self._assistant_stream_open:
                await chat.end_assistant_message()
                self._assistant_stream_open = False
            await chat.add_done_marker()
        except asyncio.CancelledError:
            if self._assistant_stream_open:
                await chat.end_assistant_message()
                self._assistant_stream_open = False
            await chat.add_system_message("[dim]（Run 恢复已取消）[/]")
        except Exception as e:
            await chat.add_system_message(f"[bold red]恢复失败: {escape(str(e))}[/]")
        finally:
            status.set_main_thinking(False)
            self._processing = False
            self._stream_task = None
            self._notify_spinner()
            self.query_one(InputAreaType).focus()

    async def _cancel_current_run(self) -> None:
        chat = self.query_one(ChatArea)
        if self._current_run is None:
            await chat.add_system_message("[dim]当前没有运行中的 Run[/]")
            return
        self._harness.cancel(self._current_run.run_id)
        await chat.add_system_message(f"[dim]已请求取消 Run {self._current_run.run_id[:8]}[/]")

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
        elif isinstance(event, AgentCompletionEvent):
            await chat.end_assistant_message()
            await chat.add_agent_notification(
                agent_id=event.agent_id,
                task_id=event.task_id,
                success=event.success,
                output=event.output + ("\n[结果可能过期]" if event.is_stale else ""),
            )
            await chat.begin_assistant_message()
            self._refresh_agent_count()
        elif isinstance(event, CompactOccurred):
            label = COMPACT_LABELS.get(event.reason, "对话已压缩")
            await chat.end_assistant_message()
            await chat.add_system_message(f"[dim]（{label}）[/]")
            await chat.begin_assistant_message()

    def _refresh_agent_count(self) -> None:
        count = self._runtime.active_agent_count
        self.query_one(StatusBar).update_agent_count(count)
        self._notify_spinner()

    async def _on_query_event(self, event: Event, step: Step, run_state: RunState) -> None:
        chat = self.query_one(ChatArea)
        if self._current_text_step_id != step.id:
            self._current_text_step_id = step.id
            self._current_step_had_output = False
        if not self._assistant_stream_open:
            await chat.begin_assistant_message()
            self._assistant_stream_open = True
        if isinstance(event, TextDelta) and event.content:
            self._current_step_had_output = True
        await self._handle_event(event, chat)
        self._update_run_status(run_state, step.title)

    async def _on_harness_event(self, event: HarnessEvent, run_state: RunState) -> None:
        self._current_run = run_state
        step_title = ""
        if run_state.current_step_id is not None:
            step = run_state.get_step(run_state.current_step_id)
            if step is not None:
                step_title = step.title
        self._update_run_status(run_state, step_title)

        chat = self.query_one(ChatArea)
        if event.event_type == "run_started":
            await chat.add_system_message(f"[dim]启动 Run {run_state.run_id[:8]}[/]")
        elif event.event_type == "run_completed":
            if self._assistant_stream_open:
                await chat.end_assistant_message()
                self._assistant_stream_open = False
            await chat.add_system_message(f"[dim]Run {run_state.run_id[:8]} 完成[/]")
        elif event.event_type == "step_started":
            step_key = (run_state.run_id, event.step_id or "")
            if step_key != self._last_announced_step_key:
                step = run_state.get_step(event.step_id or "")
                retry_suffix = ""
                if step is not None and step.retry_count > 0:
                    retry_suffix = f" [dim](重试 {step.retry_count + 1})[/]"
                await chat.add_system_message(f"[dim]▶ {event.message}[/]{retry_suffix}")
                self._last_announced_step_key = step_key
            self._current_text_step_id = event.step_id
            self._current_step_had_output = False
        elif event.event_type == "step_completed":
            step = run_state.get_step(event.step_id or "")
            if step is not None and step.kind in {
                StepKind.BOOTSTRAP_PROJECT,
                StepKind.ANALYZE_REPO,
                StepKind.MAKE_PLAN,
                StepKind.EDIT_CODE,
                StepKind.SUMMARIZE_PROGRESS,
                StepKind.FINALIZE,
            }:
                if not self._current_step_had_output and step.summary.strip():
                    await chat.begin_assistant_message()
                    await chat.append_assistant_text(step.summary)
                    await chat.end_assistant_message()
                if self._assistant_stream_open:
                    await chat.end_assistant_message()
                    self._assistant_stream_open = False
                self._current_text_step_id = None
                self._current_step_had_output = False
        elif event.event_type in {"run_failed", "run_timed_out"}:
            if self._assistant_stream_open:
                await chat.end_assistant_message()
                self._assistant_stream_open = False
            await chat.add_system_message(f"[bold red]{escape(event.message)}[/]")

    def _update_run_status(self, run_state: RunState, step_title: str = "") -> None:
        self.query_one(StatusBar).update_run(
            run_id=run_state.run_id,
            status=run_state.status.value,
            phase=run_state.phase,
            step_title=step_title,
        )

    def _refresh_engine_context(self) -> None:
        self._rebuild_system_message()

    def _attach_run_context(self, run: RunState) -> None:
        self._current_run = run
        self._runtime.current_run_id = run.run_id
        self._rebuild_system_message()
        self._update_run_status(run)

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from mini_cc.app.tui.theme import DEFAULT_THEME
from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.models import RunState, RunStatus, StepStatus, TraceSpan, format_local_time
from mini_cc.harness.runner import RunHarness

_T = DEFAULT_THEME

_RUN_STATUS_ICONS: dict[RunStatus, str] = {
    RunStatus.CREATED: "⏳",
    RunStatus.PLANNING: "✎",
    RunStatus.RUNNING: "▶",
    RunStatus.COOLDOWN: "⏸",
    RunStatus.VERIFYING: "✓",
    RunStatus.BLOCKED: "⚠",
    RunStatus.WAITING_HUMAN: "…",
    RunStatus.COMPLETED: "✓",
    RunStatus.FAILED: "✗",
    RunStatus.CANCELLED: "■",
    RunStatus.TIMED_OUT: "⌛",
}

_RUN_STATUS_COLORS: dict[RunStatus, str] = {
    RunStatus.CREATED: "#d29922",
    RunStatus.PLANNING: "#58a6ff",
    RunStatus.RUNNING: "#238636",
    RunStatus.COOLDOWN: "#8b949e",
    RunStatus.VERIFYING: "#1f6feb",
    RunStatus.BLOCKED: "#d29922",
    RunStatus.WAITING_HUMAN: "#8b949e",
    RunStatus.COMPLETED: "#3fb950",
    RunStatus.FAILED: "#da3633",
    RunStatus.CANCELLED: "#8b949e",
    RunStatus.TIMED_OUT: "#da3633",
}

_STEP_STATUS_ICONS: dict[StepStatus, str] = {
    StepStatus.PENDING: "·",
    StepStatus.IN_PROGRESS: "▶",
    StepStatus.SUCCEEDED: "✓",
    StepStatus.FAILED_RETRYABLE: "↺",
    StepStatus.FAILED_TERMINAL: "✗",
    StepStatus.SKIPPED: "○",
}


RunSelectedCallback = Callable[[RunState], Awaitable[None] | None]


class RunScreen(Screen[None]):
    DEFAULT_CSS = f"""
    RunScreen {{
        layout: vertical;
        background: $surface;
    }}
    RunScreen #run-header {{
        height: 1;
        width: 1fr;
        padding: 0 2;
        background: {_T.status_bg};
        color: $text;
        content-align: left middle;
    }}
    RunScreen #run-list {{
        height: 1fr;
        width: 1fr;
        padding: 1 2;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }}
    RunScreen #detail-area {{
        height: auto;
        max-height: 70%;
        width: 1fr;
        padding: 1 2;
        border-top: tall {_T.tool_border};
        background: $boost;
        display: none;
    }}
    RunScreen #detail-area.visible {{
        display: block;
    }}
    """

    BINDINGS = [
        Binding("escape", "back", "返回聊天", show=True),
        Binding("up", "cursor_up", "上移", show=False),
        Binding("down", "cursor_down", "下移", show=False),
        Binding("enter", "view_detail", "查看详情", show=True),
        Binding("r", "refresh", "刷新", show=True),
        Binding("s", "resume_run", "恢复 Run", show=True),
        Binding("c", "cancel_run", "取消 Run", show=True),
    ]

    def __init__(
        self,
        store: CheckpointStore,
        harness: RunHarness | None = None,
        on_run_selected: RunSelectedCallback | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._harness = harness
        self._on_run_selected = on_run_selected
        self._runs: list[RunState] = []
        self._selected_idx = -1
        self._detail_visible = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("  Run Timeline", id="run-header")
        yield Vertical(
            Static("", id="run-list"),
            Static("", id="detail-area"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._refresh()

    def action_cursor_up(self) -> None:
        if not self._runs:
            return
        if self._selected_idx <= 0:
            self._selected_idx = len(self._runs) - 1
        else:
            self._selected_idx -= 1
        self._try_render()

    def action_cursor_down(self) -> None:
        if not self._runs:
            return
        if self._selected_idx >= len(self._runs) - 1:
            self._selected_idx = 0
        else:
            self._selected_idx += 1
        self._try_render()

    def action_view_detail(self) -> None:
        if self._selected_idx < 0 or self._selected_idx >= len(self._runs):
            return
        run = self._runs[self._selected_idx]
        self._show_detail(run)
        self.call_after_refresh(self._notify_run_selected, run)

    async def action_resume_run(self) -> None:
        if self._harness is None:
            return
        if self._selected_idx < 0 or self._selected_idx >= len(self._runs):
            return
        run = self._runs[self._selected_idx]
        if run.is_terminal:
            return
        await self._harness.resume(run.run_id)
        self._refresh()
        if self._selected_idx >= 0:
            self.call_after_refresh(self._notify_run_selected, self._runs[self._selected_idx])

    def action_cancel_run(self) -> None:
        if self._harness is None:
            return
        if self._selected_idx < 0 or self._selected_idx >= len(self._runs):
            return
        run = self._runs[self._selected_idx]
        if run.status in {RunStatus.CREATED, RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.VERIFYING}:
            self._harness.cancel(run.run_id)
            self._refresh()

    def _refresh(self) -> None:
        self._runs = self._store.list_states()
        if self._selected_idx >= len(self._runs):
            self._selected_idx = len(self._runs) - 1
        if not self._runs:
            self._selected_idx = -1
            self._detail_visible = False
        self._try_render()

    def _try_render(self) -> None:
        try:
            self._render_list()
            if self._detail_visible and self._selected_idx >= 0:
                self._show_detail(self._runs[self._selected_idx])
            else:
                detail = self.query_one("#detail-area", Static)
                detail.set_class(False, "visible")
        except Exception:
            pass

    def _render_list(self) -> None:
        list_widget = self.query_one("#run-list", Static)
        if not self._runs:
            list_widget.update("[dim]暂无 Run[/]")
            return

        lines: list[str] = []
        for index, run in enumerate(self._runs):
            icon = _RUN_STATUS_ICONS.get(run.status, "?")
            color = _RUN_STATUS_COLORS.get(run.status, "white")
            selected = " [dim]◀[/]" if index == self._selected_idx else ""
            summary = run.latest_summary[:60] if run.latest_summary else run.goal[:60]
            lines.append(
                f"[{color}]{icon}[/] [bold #58a6ff]{run.run_id[:8]}[/]"
                f"  [{color}]{run.status.value}[/]"
                f"  [dim]{run.phase}[/]"
                f"  [dim]{summary}[/]"
                f"{selected}"
            )

        list_widget.update("\n".join(lines))

    def _show_detail(self, run: RunState) -> None:
        detail = self.query_one("#detail-area", Static)
        self._detail_visible = True
        detail.set_class(True, "visible")

        step_lines = self._step_lines(run)
        artifact_lines = self._artifact_lines(run)
        event_lines = self._event_lines(run.run_id)
        review_lines = self._review_lines(run.run_id)
        scheduler_lines = self._scheduler_lines(run.run_id)
        trace_lines = self._trace_lines(run.run_id)
        journal_lines = self._journal_lines(run.run_id)
        documentation_lines = self._documentation_lines(run.run_id)

        parts = [
            f"[bold #58a6ff]Run {run.run_id}[/]",
            f"  状态: {_RUN_STATUS_ICONS.get(run.status, '?')} {run.status.value}",
            f"  Phase: {run.phase}",
            f"  Goal: {run.goal}",
            f"  Created: {format_local_time(run.created_at)}",
            f"  Updated: {format_local_time(run.updated_at)}",
            f"  Current Step: {run.current_step_id or '(无)'}",
            f"  Failures: {run.failure_count}",
            f"  No Progress: {run.consecutive_no_progress_count}",
            "",
            "[bold]Steps:[/]",
            *step_lines,
        ]

        if artifact_lines:
            parts.extend(["", "[bold]Artifacts:[/]", *artifact_lines])
        if review_lines:
            parts.extend(["", "[bold]Iteration Reviews:[/]", *review_lines])
        if scheduler_lines:
            parts.extend(["", "[bold]Scheduler:[/]", *scheduler_lines])
        if trace_lines:
            parts.extend(["", "[bold]Trace:[/]", *trace_lines])
        if journal_lines:
            parts.extend(["", "[bold]Journal Tail:[/]", *journal_lines])
        if documentation_lines:
            parts.extend(["", "[bold]Documentation:[/]", *documentation_lines])
        if event_lines:
            parts.extend(["", "[bold]Recent Events:[/]", *event_lines])

        detail.update("\n".join(parts))

    def _step_lines(self, run: RunState) -> list[str]:
        if not run.steps:
            return ["  [dim](无步骤)[/]"]
        lines: list[str] = []
        for step in run.steps:
            icon = _STEP_STATUS_ICONS.get(step.status, "?")
            summary = escape((step.summary[:80] if step.summary else step.goal)[:80])
            lines.append(f"  {icon} [bold]{step.title}[/] [dim]{step.status.value}[/] [dim]{summary}[/]")
        return lines

    def _artifact_lines(self, run: RunState) -> list[str]:
        if not run.artifacts:
            return []
        lines: list[str] = []
        for name, path in sorted(run.artifacts.items()):
            lines.append(f"  [bold]{name}[/] [dim]{path}[/]")
            preview = self._read_preview(Path(path))
            if preview:
                lines.append(f"    [dim]{preview}[/]")
        return lines

    def _event_lines(self, run_id: str) -> list[str]:
        events = self._store.load_events(run_id)
        if not events:
            return []
        lines: list[str] = []
        for event in events[-8:]:
            step_label = f" [{event.step_id}]" if event.step_id else ""
            details: list[str] = []
            if "decision" in event.data:
                details.append(f"decision={event.data['decision']}")
            if "scheduler_considered" in event.data:
                details.append(f"considered={event.data['scheduler_considered']}")
            if "scheduler_rejected" in event.data and event.data["scheduler_rejected"]:
                details.append(f"rejected={event.data['scheduler_rejected']}")
            if "invalidated_agents" in event.data:
                details.append(f"invalidated_agents={event.data['invalidated_agents']}")
            if "trace_span_count" in event.data:
                details.append(f"trace_spans={event.data['trace_span_count']}")
            if "trace_tool_count" in event.data:
                details.append(f"tools={event.data['trace_tool_count']}")
            if "trace_agent_count" in event.data:
                details.append(f"agents={event.data['trace_agent_count']}")
            if "trace_elapsed_ms" in event.data:
                details.append(f"elapsed={self._format_trace_duration_str(event.data['trace_elapsed_ms'])}")
            if "trace_first_event_ms" in event.data:
                details.append(f"first_event={self._format_trace_duration_str(event.data['trace_first_event_ms'])}")
            if "trace_first_token_ms" in event.data:
                details.append(f"first_token={self._format_trace_duration_str(event.data['trace_first_token_ms'])}")
            suffix = f" [dim]({' '.join(details)})[/]" if details else ""
            lines.append(f"  [dim]{event.timestamp}[/] [bold]{event.event_type}[/]{step_label} {event.message}{suffix}")
        return lines

    def _review_lines(self, run_id: str) -> list[str]:
        reviews = self._store.load_iteration_reviews(run_id)
        if not reviews:
            return []
        lines: list[str] = []
        for review in reviews[-5:]:
            recommendation = review.recommended_step_kind or "none"
            lines.append(
                f"  [bold]{review.step_id}[/] [dim]{review.outcome.value}[/] "
                f"[dim]{review.root_cause[:90]}[/] [dim]next={recommendation}[/]"
            )
        return lines

    def _trace_lines(self, run_id: str) -> list[str]:
        spans = self._store.load_trace_spans(run_id)
        if not spans:
            return []
        children: dict[str, list[TraceSpan]] = {}
        roots: list[TraceSpan] = []
        for span in spans:
            if span.parent_span_id is None:
                roots.append(span)
                continue
            children.setdefault(span.parent_span_id, []).append(span)
        lines: list[str] = []
        for root in roots[-2:]:
            lines.extend(self._render_trace_span(root, children, indent=0))
        return lines

    def _scheduler_lines(self, run_id: str) -> list[str]:
        decisions = self._store.load_scheduler_decisions(run_id)
        if not decisions:
            return []
        lines: list[str] = []
        for decision in decisions[-5:]:
            rejected = ",".join(decision.rejected_targets) or "-"
            lines.append(
                f"  [bold]{decision.step_id}[/] [dim]role={decision.selected_role} "
                f"priority={decision.selected_priority} considered={decision.considered_count}[/]"
            )
            lines.append(f"    [dim]rejected={rejected}[/]")
            lines.append(f"    [dim]{escape(decision.reason[:100])}[/]")
        return lines

    def _journal_lines(self, run_id: str) -> list[str]:
        journal_path = self._store.journal_path(run_id)
        if not journal_path.is_file():
            return []
        try:
            content = journal_path.read_text(encoding="utf-8")
        except OSError:
            return []
        raw_lines = [line.strip() for line in content.splitlines() if line.strip()]
        return [f"  [dim]{line[:120]}[/]" for line in raw_lines[-8:]]

    def _documentation_lines(self, run_id: str) -> list[str]:
        doc_path = self._store.documentation_path(run_id)
        if not doc_path.is_file():
            return []
        try:
            content = doc_path.read_text(encoding="utf-8")
        except OSError:
            return []
        raw_lines = [line.strip() for line in content.splitlines() if line.strip()]
        return [f"  [dim]{line[:120]}[/]" for line in raw_lines[:8]]

    def _render_trace_span(
        self,
        span: TraceSpan,
        children: dict[str, list[TraceSpan]],
        *,
        indent: int,
    ) -> list[str]:
        prefix = "  " + "  " * indent
        duration = self._format_trace_duration(span.duration_ms)
        summary = escape(span.summary[:80]) if span.summary else ""
        line = f"{prefix}[bold]{span.kind}:{span.name}[/] [dim]{span.status} {duration}[/]"
        if summary:
            line += f" [dim]{summary}[/]"
        lines = [line]
        for child in children.get(span.span_id, []):
            lines.extend(self._render_trace_span(child, children, indent=indent + 1))
        return lines

    def _format_trace_duration(self, duration_ms: int | None) -> str:
        if duration_ms is None:
            return "-"
        if duration_ms >= 1000:
            return f"{duration_ms / 1000:.1f}s"
        return f"{duration_ms}ms"

    def _format_trace_duration_str(self, duration_ms: str) -> str:
        try:
            return self._format_trace_duration(int(duration_ms))
        except ValueError:
            return duration_ms

    def _read_preview(self, path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        preview = text[:160].replace("\n", " ")
        return preview + ("..." if len(text) > 160 else "")

    def _notify_run_selected(self, run: RunState) -> None:
        if self._on_run_selected is None:
            return
        result = self._on_run_selected(run)
        if inspect.isawaitable(result):
            self.run_worker(result)

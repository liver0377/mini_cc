from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from mini_cc.agent.dispatcher import AgentDispatcher, AgentDispatchRequest
from mini_cc.context.engine_context import EngineContext
from mini_cc.harness.dispatch_roles import role_for_step
from mini_cc.harness.models import AgentBudget, FailureClass, RunState, Step, StepKind, StepResult, TraceSpan, WorkItem
from mini_cc.models import (
    AgentCompletionEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    Message,
    QueryState,
    Role,
    TextDelta,
    ToolCallDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.tools.bash import Bash

if TYPE_CHECKING:
    from mini_cc.agent.manager import AgentManager

StepHandler = Callable[[Step, RunState], Awaitable[StepResult]]
QueryEventSink = Callable[[Event, Step, RunState], Awaitable[None] | None]


class _QueryDiagnostics:
    __slots__ = (
        "started_at",
        "last_event_at",
        "last_event_type",
        "first_event_at",
        "first_text_delta_at",
        "text_delta_count",
        "tool_call_names",
        "tool_result_count",
        "agent_event_count",
        "total_text_chars",
        "turn_count",
        "message_count",
        "turn_starts",
        "turn_llm_durations",
        "turn_tool_durations",
        "current_turn_start",
        "current_turn_phase",
        "current_turn_phase_start",
        "active_tool_calls",
        "tool_trace",
        "active_agent_tools",
        "agent_started_at",
        "max_inter_event_gap",
        "prev_event_at",
        "error_type",
        "error_detail",
        "trace_spans",
    )

    def __init__(self, message_count: int = 0, turn_count: int = 0) -> None:
        self.started_at: float = 0.0
        self.last_event_at: float = 0.0
        self.last_event_type: str = ""
        self.first_event_at: float = 0.0
        self.first_text_delta_at: float = 0.0
        self.text_delta_count: int = 0
        self.tool_call_names: list[str] = []
        self.tool_result_count: int = 0
        self.agent_event_count: int = 0
        self.total_text_chars: int = 0
        self.turn_count: int = turn_count
        self.message_count: int = message_count
        self.turn_starts: list[float] = []
        self.turn_llm_durations: list[float] = []
        self.turn_tool_durations: list[float] = []
        self.current_turn_start: float = 0.0
        self.current_turn_phase: str = ""
        self.current_turn_phase_start: float = 0.0
        self.active_tool_calls: dict[str, tuple[str, float, str]] = {}
        self.tool_trace: list[str] = []
        self.active_agent_tools: dict[str, list[tuple[str, float]]] = {}
        self.agent_started_at: dict[str, float] = {}
        self.max_inter_event_gap: float = 0.0
        self.prev_event_at: float = 0.0
        self.error_type: str = ""
        self.error_detail: str = ""
        self.trace_spans: list[TraceSpan] = []

    def record_event(self, event: Event) -> None:
        now = time.monotonic()
        if self.first_event_at == 0.0:
            self.first_event_at = now
        if self.prev_event_at > 0.0:
            gap = now - self.prev_event_at
            if gap > self.max_inter_event_gap:
                self.max_inter_event_gap = gap
        self.prev_event_at = now
        self.last_event_at = now
        self.last_event_type = type(event).__name__
        if isinstance(event, TextDelta):
            if self.first_text_delta_at == 0.0:
                self.first_text_delta_at = now
            self.text_delta_count += 1
            self.total_text_chars += len(event.content)
            if self.current_turn_phase == "tool":
                self._finish_turn_inner(now)
            if self.current_turn_phase == "":
                self.current_turn_phase = "llm"
                self.current_turn_start = now
                self.turn_starts.append(now)
                self.current_turn_phase_start = now
        elif isinstance(event, ToolResultEvent):
            self.tool_result_count += 1
            self.tool_call_names.append(event.name)
            self._record_tool_result(event, now)
            if self.current_turn_phase == "llm":
                llm_dur = now - self.current_turn_start
                self.turn_llm_durations.append(llm_dur)
                self.current_turn_phase = "tool"
                self.current_turn_phase_start = now
        elif isinstance(event, ToolCallStart):
            self.active_tool_calls[event.tool_call_id] = (event.name, now, "")
            if self.current_turn_phase == "":
                self.current_turn_phase = "llm"
                self.current_turn_start = now
                self.turn_starts.append(now)
                self.current_turn_phase_start = now
        elif isinstance(event, ToolCallDelta):
            existing = self.active_tool_calls.get(event.tool_call_id)
            if existing is not None:
                name, started_at, arguments = existing
                self.active_tool_calls[event.tool_call_id] = (name, started_at, arguments + event.arguments_json_delta)
        elif isinstance(event, AgentStartEvent):
            self.agent_event_count += 1
            self.agent_started_at[event.agent_id] = now
            self.tool_trace.append(f"agent[{event.agent_id[:8]}].start")
        elif isinstance(event, AgentToolCallEvent):
            self.agent_event_count += 1
            self.active_agent_tools.setdefault(event.agent_id, []).append((event.tool_name, now))
        elif isinstance(event, AgentToolResultEvent):
            self.agent_event_count += 1
            self._record_agent_tool_result(event, now)
        elif isinstance(event, AgentCompletionEvent):
            self.agent_event_count += 1
            started_at = self.agent_started_at.pop(event.agent_id, 0.0)
            elapsed = now - started_at if started_at > 0.0 else 0.0
            self.tool_trace.append(
                f"agent[{event.agent_id[:8]}].complete(success={str(event.success).lower()},elapsed={elapsed:.1f}s)"
            )
        else:
            agent_id = cast(str | None, getattr(event, "agent_id", None))
            if agent_id is not None:
                self.agent_event_count += 1

    def _finish_turn_inner(self, now: float) -> None:
        if self.current_turn_start == 0.0:
            return
        if self.current_turn_phase == "tool":
            tool_dur = now - self.current_turn_phase_start
            self.turn_tool_durations.append(tool_dur)
            if len(self.turn_llm_durations) < len(self.turn_starts):
                self.turn_llm_durations.append(0.0)
        elif self.current_turn_phase == "llm":
            llm_dur = now - self.current_turn_start
            self.turn_llm_durations.append(llm_dur)
            self.turn_tool_durations.append(0.0)
        self.current_turn_start = now
        self.current_turn_phase = ""
        self.current_turn_phase_start = 0.0

    def finish_turn(self) -> None:
        if self.current_turn_start == 0.0:
            return
        self._finish_turn_inner(time.monotonic())

    def to_metadata(self, timeout_seconds: int | None = None) -> dict[str, str]:
        elapsed_end = self.last_event_at if self.last_event_at > 0.0 else None
        md: dict[str, str] = {
            "trace_elapsed_ms": str(self._duration_ms(self.started_at, elapsed_end)),
            "trace_last_event_type": self.last_event_type or "(none)",
            "trace_text_delta_count": str(self.text_delta_count),
            "trace_tool_result_count": str(self.tool_result_count),
            "trace_agent_event_count": str(self.agent_event_count),
            "trace_total_text_chars": str(self.total_text_chars),
            "trace_message_count": str(self.message_count),
            "trace_turn_count": str(self.turn_count),
            "trace_max_inter_event_gap_ms": str(max(0, int(self.max_inter_event_gap * 1000))),
        }
        if timeout_seconds is not None:
            md["timeout_seconds"] = str(timeout_seconds)
        if self.first_event_at > 0.0 and self.started_at > 0.0:
            md["trace_first_event_ms"] = str(self._duration_ms(self.started_at, self.first_event_at))
        if self.first_text_delta_at > 0.0 and self.started_at > 0.0:
            md["trace_first_token_ms"] = str(self._duration_ms(self.started_at, self.first_text_delta_at))
        if self.turn_llm_durations:
            md["trace_turn_llm_ms"] = ",".join(str(max(0, int(d * 1000))) for d in self.turn_llm_durations)
        if self.turn_tool_durations:
            md["trace_turn_tool_ms"] = ",".join(str(max(0, int(d * 1000))) for d in self.turn_tool_durations)
        if self.tool_trace:
            md["trace_tool_span_count"] = str(len(self.tool_trace))
            md["trace_tool_outline"] = self._fmt_tool_trace()
        if self.tool_call_names:
            md["trace_tool_names"] = ",".join(self.tool_call_names)
        if self.error_type:
            md["trace_error_type"] = self.error_type
        if self.error_detail:
            md["trace_error_detail"] = self.error_detail[:500]
        return md

    def _fmt_elapsed(self) -> str:
        if self.started_at == 0.0:
            return ""
        end = self.last_event_at if self.last_event_at > 0.0 else time.monotonic()
        return f"{end - self.started_at:.1f}s"

    def _duration_ms(self, start: float, end: float | None) -> int:
        if start <= 0.0:
            return 0
        final_end = end if end is not None and end > 0.0 else time.monotonic()
        return max(0, int((final_end - start) * 1000))

    def summarize_timeout(self, timeout_seconds: int) -> str:
        elapsed = self._fmt_elapsed()
        turn_summary = self._fmt_turn_summary()
        if self.last_event_type == "":
            return (
                f"Step timed out after {timeout_seconds}s. "
                f"LLM provider never returned any event (elapsed: {elapsed}). "
                f"Context had {self.message_count} messages, {self.turn_count} prior turns."
            )
        if self.text_delta_count == 0 and self.tool_result_count == 0:
            return (
                f"Step timed out after {timeout_seconds}s (elapsed: {elapsed}). "
                f"No text or tool output received. "
                f"Last event: {self.last_event_type}. "
                f"Agent events: {self.agent_event_count}. "
                f"Context had {self.message_count} messages. "
                f"Max inter-event gap: {self.max_inter_event_gap:.1f}s."
            )
        return (
            f"Step timed out after {timeout_seconds}s (elapsed: {elapsed}). "
            f"Received {self.text_delta_count} text deltas ({self.total_text_chars} chars), "
            f"{self.tool_result_count} tool results ({','.join(self.tool_call_names)}). "
            f"Last event: {self.last_event_type}. "
            f"Context: {self.message_count} messages. "
            f"Max inter-event gap: {self.max_inter_event_gap:.1f}s. "
            f"{turn_summary}"
            f"{self._fmt_tool_trace_for_summary()}"
        )

    def _fmt_turn_summary(self) -> str:
        if not self.turn_llm_durations:
            return ""
        parts: list[str] = []
        for i, llm_d in enumerate(self.turn_llm_durations):
            tool_d = self.turn_tool_durations[i] if i < len(self.turn_tool_durations) else 0.0
            parts.append(f"turn{i + 1}(llm={llm_d:.1f}s,tool={tool_d:.1f}s)")
        return "Per-turn timing: " + " -> ".join(parts)

    def _record_tool_result(self, event: ToolResultEvent, now: float) -> None:
        existing = self.active_tool_calls.pop(event.tool_call_id, None)
        if existing is None:
            self.tool_trace.append(f"{event.name}=unknown")
            self.trace_spans.append(
                TraceSpan(
                    span_id=f"tool-{len(self.trace_spans) + 1}",
                    run_id="",
                    kind="tool",
                    name=event.name,
                    status="success" if event.success else "failed",
                    end_at=datetime.now(UTC).isoformat(),
                    duration_ms=0,
                )
            )
            return
        name, started_at, arguments = existing
        elapsed = now - started_at
        label = self._tool_label(name, arguments)
        self.tool_trace.append(f"{label}={elapsed:.1f}s")
        self.trace_spans.append(
            TraceSpan(
                span_id=f"tool-{len(self.trace_spans) + 1}",
                run_id="",
                kind="tool",
                name=label,
                status="success" if event.success else "failed",
                end_at=datetime.now(UTC).isoformat(),
                duration_ms=int(elapsed * 1000),
            )
        )

    def _record_agent_tool_result(self, event: AgentToolResultEvent, now: float) -> None:
        active = self.active_agent_tools.get(event.agent_id, [])
        index = next((i for i, item in enumerate(active) if item[0] == event.tool_name), -1)
        if index >= 0:
            _, started_at = active.pop(index)
            elapsed = now - started_at
        else:
            elapsed = 0.0
        if not active:
            self.active_agent_tools.pop(event.agent_id, None)
        agent_key = event.agent_id[:8]
        self.tool_trace.append(
            f"agent[{agent_key}].{event.tool_name}(success={str(event.success).lower()},elapsed={elapsed:.1f}s)"
        )
        self.trace_spans.append(
            TraceSpan(
                span_id=f"agent-tool-{len(self.trace_spans) + 1}",
                run_id="",
                kind="tool",
                name=f"agent[{agent_key}].{event.tool_name}",
                status="success" if event.success else "failed",
                end_at=datetime.now(UTC).isoformat(),
                duration_ms=int(elapsed * 1000),
                metadata={"agent_id": event.agent_id},
            )
        )

    def _tool_label(self, name: str, arguments: str) -> str:
        compact = arguments.strip()
        if not compact:
            return name
        try:
            parsed = json.loads(compact)
        except (json.JSONDecodeError, ValueError):
            return name
        if not isinstance(parsed, dict):
            return name
        for key in ("file_path", "path", "command", "pattern", "prompt"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                normalized = value.strip().replace("\n", " ")
                return f"{name}({normalized[:60]})"
        if name == "agent":
            readonly = parsed.get("readonly")
            if isinstance(readonly, bool):
                return f"{name}(readonly={str(readonly).lower()})"
        return name

    def _fmt_tool_trace(self) -> str:
        trace = " -> ".join(self.tool_trace)
        return trace[:4000]

    def _fmt_tool_trace_for_summary(self) -> str:
        if not self.tool_trace:
            return ""
        trace = self._fmt_tool_trace()
        return f" Trace: {trace}."

    def build_trace_spans(
        self,
        *,
        run_id: str,
        step_id: str,
        work_item_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> list[TraceSpan]:
        return [
            span.model_copy(
                update={
                    "span_id": f"{step_id}-{work_item_id or 'step'}-trace-{index}",
                    "run_id": run_id,
                    "step_id": step_id,
                    "work_item_id": work_item_id,
                    "parent_span_id": parent_span_id,
                }
            )
            for index, span in enumerate(self.trace_spans, start=1)
        ]


class StepRunner:
    def __init__(
        self,
        *,
        engine_ctx: EngineContext | None = None,
        handlers: dict[StepKind, StepHandler] | None = None,
        bash_tool: Bash | None = None,
        query_event_sink: QueryEventSink | None = None,
    ) -> None:
        self._engine_ctx = engine_ctx
        self._handlers = handlers or {}
        self._bash_tool = bash_tool or Bash()
        self._query_event_sink = query_event_sink
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._interrupt_event: threading.Event | None = None
        self._diag: _QueryDiagnostics | None = None

    def register_handler(self, kind: StepKind, handler: StepHandler) -> None:
        self._handlers[kind] = handler

    async def run_step(self, step: Step, run_state: RunState) -> StepResult:
        timeout_seconds = self._step_timeout_seconds(step, run_state)
        interrupt_event = self._interrupt_event
        self._diag = None
        scope: AbstractContextManager[None] = nullcontext()
        if self._engine_ctx is not None:
            self._inject_agent_budget(run_state)
            scope = self._engine_ctx.execution_scope(
                run_id=run_state.run_id,
                mode="build" if step.kind in {StepKind.BOOTSTRAP_PROJECT, StepKind.EDIT_CODE} else "plan",
                agent_budget=self._engine_ctx.agent_budget,
                interrupt_event=interrupt_event,
            )
        else:
            scope = nullcontext()
        with scope:
            try:
                return await asyncio.wait_for(self._run_step_inner(step, run_state), timeout=timeout_seconds)
            except TimeoutError:
                return self._build_timeout_result(timeout_seconds)

    async def run_work_item(self, step: Step, work_item: WorkItem, run_state: RunState) -> StepResult:
        timeout_seconds = work_item.budget_seconds or self._step_timeout_seconds(step, run_state)
        interrupt_event = self._interrupt_event
        self._diag = None
        scope: AbstractContextManager[None] = nullcontext()
        if self._engine_ctx is not None:
            self._inject_agent_budget(run_state)
            mode = "build" if work_item.role == "implementer" else "plan"
            scope = self._engine_ctx.execution_scope(
                run_id=run_state.run_id,
                mode=mode,
                agent_budget=self._engine_ctx.agent_budget,
                interrupt_event=interrupt_event,
            )
        with scope:
            try:
                return await asyncio.wait_for(
                    self._run_work_item_inner(step, work_item, run_state),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                return self._build_timeout_result(timeout_seconds)

    def _build_timeout_result(self, timeout_seconds: int) -> StepResult:
        diag = self._diag
        error = diag.summarize_timeout(timeout_seconds) if diag else f"Step timed out after {timeout_seconds}s"
        metadata = diag.to_metadata(timeout_seconds) if diag else {"timeout_seconds": str(timeout_seconds)}
        return StepResult(
            success=False,
            summary="",
            retryable=True,
            error=error,
            timed_out=True,
            progress_made=False,
            failure_class=FailureClass.TIME_BUDGET_EXCEEDED,
            metadata=metadata,
        )

    async def _run_step_inner(self, step: Step, run_state: RunState) -> StepResult:
        handler = self._handlers.get(step.kind)
        if handler is not None:
            return await handler(step, run_state)

        if step.kind in {
            StepKind.BOOTSTRAP_PROJECT,
            StepKind.ANALYZE_REPO,
            StepKind.MAKE_PLAN,
            StepKind.EDIT_CODE,
            StepKind.SUMMARIZE_PROGRESS,
            StepKind.FINALIZE,
        }:
            delegated = await self._run_delegated_agent_step(step, run_state)
            if delegated is not None:
                return delegated

        if step.kind in {
            StepKind.MAKE_PLAN,
            StepKind.SUMMARIZE_PROGRESS,
            StepKind.FINALIZE,
        }:
            return await self._run_query_step(step, run_state)
        if step.kind in {StepKind.BOOTSTRAP_PROJECT, StepKind.ANALYZE_REPO, StepKind.EDIT_CODE}:
            return await self._run_query_step(step, run_state)
        if step.kind in {StepKind.RUN_TESTS, StepKind.RUN_TASK_AUDIT, StepKind.INSPECT_FAILURES}:
            return await self._run_bash_step(step, run_state)
        if step.kind == StepKind.CHECKPOINT:
            return StepResult(success=True, summary=f"Checkpoint requested by step {step.id}", progress_made=False)
        if step.kind == StepKind.SPAWN_READONLY_AGENT:
            return await self._spawn_readonly_agent(step, run_state)

        return StepResult(success=False, summary="", retryable=False, error=f"Unsupported step kind: {step.kind}")

    async def _run_work_item_inner(self, step: Step, work_item: WorkItem, run_state: RunState) -> StepResult:
        readonly = work_item.role in {"analyzer", "planner", "reporter", "verifier"}
        mode = "plan" if readonly else "build"
        parent_state = run_state.latest_query_state if readonly else None
        prompt = str(work_item.inputs.get("prompt", work_item.goal))

        if self._engine_ctx is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="EngineContext is required for work-item execution",
            )
        if self._engine_ctx.agent_manager is None:
            result = await self._run_query_prompt(prompt, mode, step, run_state)
            result.metadata["work_item_id"] = work_item.id
            result.metadata["work_item_kind"] = work_item.kind
            return result

        dispatcher = self._resolve_dispatcher()
        agent = await dispatcher.dispatch(
            AgentDispatchRequest(
                prompt=prompt,
                readonly=readonly,
                fork=False,
                parent_state=parent_state,
                mode=mode,
                scope_paths=[] if readonly else ["."],
                run_id=run_state.run_id,
                step_id=step.id,
                work_item_id=work_item.id,
                role=work_item.role,
            )
        )

        diag = _QueryDiagnostics(
            message_count=(len(parent_state.messages) if parent_state is not None else 0),
            turn_count=(parent_state.turn_count if parent_state is not None else 0),
        )
        diag.started_at = time.monotonic()
        self._diag = diag

        start_event = AgentStartEvent(agent_id=agent.config.agent_id, task_id=agent.task_id, prompt=prompt[:80])
        diag.record_event(start_event)
        await self._emit_query_event(start_event, step, run_state)

        text_parts: list[str] = []
        try:
            async for event in agent.run(prompt):
                if isinstance(event, TextDelta):
                    text_parts.append(event.content)
                    continue
                if isinstance(event, ToolCallStart):
                    call_event = AgentToolCallEvent(agent_id=agent.config.agent_id, tool_name=event.name)
                    diag.record_event(call_event)
                    await self._emit_query_event(call_event, step, run_state)
                    continue
                if isinstance(event, ToolResultEvent):
                    preview = event.output[:100] + ("..." if len(event.output) > 100 else "")
                    result_event = AgentToolResultEvent(
                        agent_id=agent.config.agent_id,
                        tool_name=event.name,
                        success=event.success,
                        output_preview=preview,
                    )
                    diag.record_event(result_event)
                    await self._emit_query_event(result_event, step, run_state)
        except Exception as err:
            completion = self._drain_completion_for_agent(agent.config.agent_id)
            if completion is not None:
                diag.record_event(completion)
                await self._emit_query_event(completion, step, run_state)
            diag.finish_turn()
            diag.error_type = type(err).__name__
            diag.error_detail = str(err)
            metadata = diag.to_metadata()
            metadata.update(self._read_back_budget_metadata())
            metadata["delegated_agent_id"] = agent.config.agent_id
            metadata["delegated_agent_mode"] = mode
            metadata["delegated_agent_readonly"] = str(readonly).lower()
            metadata["work_item_id"] = work_item.id
            metadata["work_item_kind"] = work_item.kind
            return StepResult(
                success=False,
                summary="",
                retryable=True,
                error=f"{type(err).__name__}: {err}",
                progress_made=bool(text_parts),
                failure_class=self._classify_exception(err),
                trace_spans=diag.build_trace_spans(
                    run_id=run_state.run_id,
                    step_id=step.id,
                    work_item_id=work_item.id,
                ),
                metadata=metadata,
            )

        completion = self._drain_completion_for_agent(agent.config.agent_id)
        if completion is not None:
            diag.record_event(completion)
            await self._emit_query_event(completion, step, run_state)
        diag.finish_turn()
        summary = "".join(text_parts).strip()
        if not summary and completion is not None:
            summary = completion.output.strip()
        metadata = diag.to_metadata()
        metadata.update(self._read_back_budget_metadata())
        metadata["delegated_agent_id"] = agent.config.agent_id
        metadata["delegated_agent_mode"] = mode
        metadata["delegated_agent_readonly"] = str(readonly).lower()
        metadata["work_item_id"] = work_item.id
        metadata["work_item_kind"] = work_item.kind
        return StepResult(
            success=True,
            summary=summary or f"Work item {work_item.id} completed via agent {agent.config.agent_id}",
            progress_made=bool(summary or completion is not None),
            trace_spans=diag.build_trace_spans(
                run_id=run_state.run_id,
                step_id=step.id,
                work_item_id=work_item.id,
            ),
            metadata=metadata,
        )

    async def _run_query_step(self, step: Step, run_state: RunState) -> StepResult:
        if self._engine_ctx is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="EngineContext is required for query-backed steps",
            )

        prompt = str(step.inputs.get("prompt", step.goal))
        mode = self._engine_ctx.mode
        return await self._run_query_prompt(prompt, mode, step, run_state)

    async def _run_query_prompt(self, prompt: str, mode: str, step: Step, run_state: RunState) -> StepResult:
        if self._engine_ctx is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="EngineContext is required for query-backed steps",
            )
        state = self._prepare_query_state(run_state.latest_query_state, mode)
        text_parts: list[str] = []
        tool_outputs: list[str] = []

        diag = _QueryDiagnostics(
            message_count=len(state.messages),
            turn_count=state.turn_count,
        )
        diag.started_at = time.monotonic()
        self._diag = diag

        try:
            async for event in self._engine_ctx.engine.submit_message(prompt, state):
                diag.record_event(event)
                await self._emit_query_event(event, step, run_state)
                if isinstance(event, TextDelta):
                    text_parts.append(event.content)
                elif isinstance(event, ToolResultEvent):
                    tool_outputs.append(event.output)
        except Exception as err:
            diag.finish_turn()
            diag.error_type = type(err).__name__
            diag.error_detail = str(err)
            return StepResult(
                success=False,
                summary="",
                retryable=True,
                error=f"{type(err).__name__}: {err}",
                query_state=state,
                failure_class=self._classify_exception(err),
                trace_spans=diag.build_trace_spans(run_id=run_state.run_id, step_id=step.id),
                metadata=diag.to_metadata(0),
            )
        diag.finish_turn()
        summary = "".join(text_parts).strip()
        if not summary and tool_outputs:
            summary = "\n\n".join(tool_outputs[:3]).strip()
        progress_made = bool(summary or tool_outputs or state.turn_count > 0)
        metadata = self._read_back_budget_metadata()
        return StepResult(
            success=True,
            summary=summary or f"Completed step {step.id}",
            progress_made=progress_made,
            query_state=state,
            trace_spans=diag.build_trace_spans(run_id=run_state.run_id, step_id=step.id),
            metadata=metadata,
        )

    async def _run_bash_step(self, step: Step, run_state: RunState) -> StepResult:
        command_value = step.inputs.get("command")
        if not isinstance(command_value, str) or not command_value.strip():
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="Bash-backed step requires a non-empty command input",
            )

        timeout_value = step.inputs.get("timeout")
        requested_timeout = timeout_value if isinstance(timeout_value, int) else 120000
        budget_timeout = self._step_timeout_seconds(step, run_state) * 1000
        timeout = min(requested_timeout, budget_timeout)
        if hasattr(self._bash_tool, "async_execute"):
            result = await self._bash_tool.async_execute(
                command=command_value,
                timeout=timeout,
                _is_interrupted=self._is_interrupted,
            )
        else:
            result = await asyncio.to_thread(self._bash_tool.execute, command=command_value, timeout=timeout)
        output = result.output or result.error or ""
        artifact_name_value = step.inputs.get("artifact_name")
        if isinstance(artifact_name_value, str) and artifact_name_value.strip():
            artifact_name = artifact_name_value.strip()
        else:
            artifact_name = f"{step.id or step.kind.value}.txt"
        artifact_key = "task_audit" if step.kind == StepKind.RUN_TASK_AUDIT else artifact_name
        metadata: dict[str, str] = {
            "command": command_value,
            "timeout_ms": str(timeout),
            "artifact_name": artifact_name,
            "audit_profile": str(step.inputs.get("profile", "")),
        }
        if step.kind == StepKind.RUN_TASK_AUDIT:
            self._enrich_audit_metadata(metadata, output)
        return StepResult(
            success=result.success,
            summary=output[:1000].strip(),
            artifacts={artifact_key: output},
            retryable=result.success is False,
            error=result.error,
            progress_made=bool(output.strip()),
            failure_class=None if result.success else FailureClass.TOOL_FAILURE,
            trace_spans=[
                TraceSpan(
                    span_id=f"{step.id}-bash",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    kind="tool",
                    name="bash",
                    status="success" if result.success else "failed",
                    end_at=datetime.now(UTC).isoformat(),
                    duration_ms=timeout,
                    summary=command_value[:120],
                )
            ],
            metadata=metadata,
        )

    def _enrich_audit_metadata(self, metadata: dict[str, str], output: str) -> None:
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(parsed, dict):
            return
        summary = parsed.get("summary")
        if isinstance(summary, dict):
            for key in ("cases_total", "cases_passed", "cases_failed"):
                value = summary.get(key)
                if value is not None:
                    metadata[f"audit_{key}"] = str(value)
            passed = summary.get("cases_passed")
            total = summary.get("cases_total")
            if isinstance(passed, int) and isinstance(total, int):
                metadata["audit_summary"] = f"{passed}/{total} semantic cases passed"
        elif isinstance(summary, str) and summary.strip():
            metadata["audit_summary"] = summary.strip()
        blockers = parsed.get("blockers")
        if isinstance(blockers, list) and blockers:
            metadata["audit_blockers"] = " | ".join(str(b) for b in blockers if str(b).strip())
        regressions = parsed.get("regressions")
        if isinstance(regressions, list) and regressions:
            metadata["audit_regressions"] = " | ".join(str(r) for r in regressions if str(r).strip())
        improvements = parsed.get("improvements")
        if isinstance(improvements, list) and improvements:
            metadata["audit_improvements"] = " | ".join(str(i) for i in improvements if str(i).strip())
        next_focus = parsed.get("recommended_next_focus")
        if isinstance(next_focus, str) and next_focus.strip():
            metadata["audit_next_focus"] = next_focus.strip()

    async def _spawn_readonly_agent(self, step: Step, run_state: RunState) -> StepResult:
        if self._engine_ctx is None or self._engine_ctx.agent_manager is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="AgentManager is required for readonly agent steps",
            )

        prompt = str(step.inputs.get("prompt", step.goal))
        dispatcher = self._resolve_dispatcher()
        try:
            agent = await dispatcher.dispatch(
                AgentDispatchRequest(
                    prompt=prompt,
                    readonly=True,
                    fork=bool(step.inputs.get("fork", False)),
                    parent_state=run_state.latest_query_state,
                    mode="plan",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    work_item_id=step.id,
                    role=role_for_step(step.kind),
                )
            )
        except Exception as err:
            return StepResult(
                success=False,
                summary="",
                retryable=True,
                error=str(err),
                failure_class=self._classify_exception(err),
            )
        task = asyncio.create_task(agent.run_background(prompt))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        metadata: dict[str, str] = {"agent_id": agent.config.agent_id}
        metadata.update(self._read_back_budget_metadata())
        return StepResult(
            success=True,
            summary=f"Readonly agent {agent.config.agent_id} started for step {step.id}",
            progress_made=True,
            trace_spans=[
                TraceSpan(
                    span_id=f"{step.id}-readonly-agent",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    work_item_id=step.id,
                    kind="agent",
                    name=agent.config.agent_id,
                    status="started",
                    end_at=datetime.now(UTC).isoformat(),
                    metadata={"mode": "plan", "readonly": "true"},
                )
            ],
            metadata=metadata,
        )

    async def _run_delegated_agent_step(self, step: Step, run_state: RunState) -> StepResult | None:
        if self._engine_ctx is None or self._engine_ctx.agent_manager is None:
            return None

        readonly = step.kind in {
            StepKind.ANALYZE_REPO,
            StepKind.MAKE_PLAN,
            StepKind.SUMMARIZE_PROGRESS,
            StepKind.FINALIZE,
        }
        prompt = str(step.inputs.get("prompt", step.goal))
        mode = "plan" if readonly else "build"
        parent_state = run_state.latest_query_state if readonly else None

        dispatcher = self._resolve_dispatcher()
        agent = await dispatcher.dispatch(
            AgentDispatchRequest(
                prompt=prompt,
                readonly=readonly,
                fork=False,
                parent_state=parent_state,
                mode=mode,
                scope_paths=[] if readonly else ["."],
                run_id=run_state.run_id,
                step_id=step.id,
                work_item_id=step.id,
                role=role_for_step(step.kind),
            )
        )

        diag = _QueryDiagnostics(
            message_count=(len(parent_state.messages) if parent_state is not None else 0),
            turn_count=(parent_state.turn_count if parent_state is not None else 0),
        )
        diag.started_at = time.monotonic()
        self._diag = diag

        start_event = AgentStartEvent(agent_id=agent.config.agent_id, task_id=agent.task_id, prompt=prompt[:80])
        diag.record_event(start_event)
        await self._emit_query_event(start_event, step, run_state)

        text_parts: list[str] = []
        try:
            async for event in agent.run(prompt):
                if isinstance(event, TextDelta):
                    text_parts.append(event.content)
                    continue
                if isinstance(event, ToolCallStart):
                    call_event = AgentToolCallEvent(agent_id=agent.config.agent_id, tool_name=event.name)
                    diag.record_event(call_event)
                    await self._emit_query_event(call_event, step, run_state)
                    continue
                if isinstance(event, ToolResultEvent):
                    preview = event.output[:100] + ("..." if len(event.output) > 100 else "")
                    result_event = AgentToolResultEvent(
                        agent_id=agent.config.agent_id,
                        tool_name=event.name,
                        success=event.success,
                        output_preview=preview,
                    )
                    diag.record_event(result_event)
                    await self._emit_query_event(result_event, step, run_state)
        except Exception as err:
            completion = self._drain_completion_for_agent(agent.config.agent_id)
            if completion is not None:
                diag.record_event(completion)
                await self._emit_query_event(completion, step, run_state)
            diag.finish_turn()
            diag.error_type = type(err).__name__
            diag.error_detail = str(err)
            metadata = diag.to_metadata()
            metadata.update(self._read_back_budget_metadata())
            metadata["delegated_agent_id"] = agent.config.agent_id
            metadata["delegated_agent_mode"] = mode
            metadata["delegated_agent_readonly"] = str(readonly).lower()
            return StepResult(
                success=False,
                summary="",
                retryable=True,
                error=f"{type(err).__name__}: {err}",
                progress_made=bool(text_parts),
                failure_class=self._classify_exception(err),
                trace_spans=diag.build_trace_spans(run_id=run_state.run_id, step_id=step.id, work_item_id=step.id),
                metadata=metadata,
            )

        completion = self._drain_completion_for_agent(agent.config.agent_id)
        if completion is not None:
            diag.record_event(completion)
            await self._emit_query_event(completion, step, run_state)
        diag.finish_turn()
        summary = "".join(text_parts).strip()
        if not summary and completion is not None:
            summary = completion.output.strip()
        metadata = diag.to_metadata()
        metadata.update(self._read_back_budget_metadata())
        metadata["delegated_agent_id"] = agent.config.agent_id
        metadata["delegated_agent_mode"] = mode
        metadata["delegated_agent_readonly"] = str(readonly).lower()
        return StepResult(
            success=True,
            summary=summary or f"Delegated step {step.id} completed via agent {agent.config.agent_id}",
            progress_made=bool(summary or completion is not None),
            trace_spans=diag.build_trace_spans(run_id=run_state.run_id, step_id=step.id, work_item_id=step.id),
            metadata=metadata,
        )

    def _drain_completion_for_agent(self, agent_id: str) -> AgentCompletionEvent | None:
        if self._engine_ctx is None or self._engine_ctx.completion_queue is None:
            return None
        queue = self._engine_ctx.completion_queue
        drained: list[AgentCompletionEvent] = []
        matched: AgentCompletionEvent | None = None
        while not queue.empty():
            try:
                event = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if event.agent_id == agent_id and matched is None:
                matched = event
            else:
                drained.append(event)
        for event in drained:
            queue.put_nowait(event)
        return matched

    async def _emit_query_event(self, event: Event, step: Step, run_state: RunState) -> None:
        if self._query_event_sink is None:
            return
        result = self._query_event_sink(event, step, run_state)
        if inspect.isawaitable(result):
            await result

    def _prepare_query_state(self, state: QueryState | None, mode: str) -> QueryState:
        if self._engine_ctx is None:
            raise RuntimeError("EngineContext is required for query state preparation")
        run_id = self._engine_ctx.current_run_id
        content = self._engine_ctx.prompt_builder.build(self._engine_ctx.env_info, mode=mode, run_id=run_id)
        if state is None:
            return QueryState(messages=[Message(role=Role.SYSTEM, content=content)])

        next_state = state.model_copy(deep=True)
        if next_state.messages and next_state.messages[0].role == Role.SYSTEM:
            next_state.messages[0] = Message(role=Role.SYSTEM, content=content)
        else:
            next_state.messages.insert(0, Message(role=Role.SYSTEM, content=content))
        return next_state

    def _inject_agent_budget(self, run_state: RunState) -> None:
        if self._engine_ctx is None:
            return
        if run_state.agent_budget is not None:
            configured = run_state.agent_budget.model_copy(deep=True)
        else:
            configured = AgentBudget(
                max_readonly=run_state.budget.max_active_agents,
                max_write=1,
                remaining_readonly=run_state.budget.max_active_agents,
                remaining_write=1,
            )
        active_readonly = run_state.active_readonly_agent_count
        active_write = run_state.active_write_agent_count
        self._engine_ctx.agent_budget = AgentBudget(
            max_readonly=configured.max_readonly,
            max_write=1,
            remaining_readonly=max(0, configured.max_readonly - active_readonly),
            remaining_write=max(0, 1 - active_write),
        )

    def _read_back_budget_metadata(self) -> dict[str, str]:
        if self._engine_ctx is None or self._engine_ctx.agent_budget is None:
            return {}
        budget: AgentBudget = self._engine_ctx.agent_budget  # type: ignore[assignment]
        return {
            "agents_remaining_ro": str(budget.remaining_readonly),
            "agents_remaining_w": str(budget.remaining_write),
        }

    def _step_timeout_seconds(self, step: Step, run_state: RunState) -> int:
        timeout = step.budget_seconds if step.budget_seconds is not None else run_state.budget.max_step_seconds
        return max(1, timeout)

    def _classify_exception(self, err: Exception) -> FailureClass:
        name = type(err).__name__.lower()
        detail = str(err).lower()
        if "ratelimit" in name or "rate limit" in detail or "429" in detail:
            return FailureClass.TRANSIENT_PROVIDER
        if isinstance(err, TimeoutError):
            return FailureClass.TIME_BUDGET_EXCEEDED
        return FailureClass.LOGIC_FAILURE

    def _resolve_dispatcher(self) -> AgentDispatcher:
        if self._engine_ctx is None or self._engine_ctx.agent_manager is None:
            raise RuntimeError("AgentManager is required for delegated agent steps")
        engine_ctx = self._engine_ctx
        dispatcher = engine_ctx.agent_dispatcher
        if dispatcher is not None:
            return dispatcher
        manager = cast("AgentManager", engine_ctx.agent_manager)
        return AgentDispatcher(
            manager=manager,
            get_budget=lambda: cast(AgentBudget | None, engine_ctx.agent_budget),
        )

    def set_step_context(self, step: Step) -> None:
        if self._engine_ctx is None:
            return
        mgr = self._engine_ctx.agent_manager
        if mgr is not None:
            mgr.set_current_step(step.id)

    def clear_step_context(self) -> None:
        if self._engine_ctx is None:
            return
        mgr = self._engine_ctx.agent_manager
        if mgr is not None:
            mgr.clear_current_step()

    def set_interrupt_event(self, interrupt_event: threading.Event | None) -> None:
        self._interrupt_event = interrupt_event

    def cancel_active_agents(self, agent_ids: list[str] | None = None) -> list[str]:
        if self._engine_ctx is None or self._engine_ctx.agent_manager is None:
            return []
        return self._engine_ctx.agent_manager.cancel_agents(agent_ids)

    def _is_interrupted(self) -> bool:
        return self._interrupt_event.is_set() if self._interrupt_event is not None else False

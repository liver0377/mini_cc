from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Any

from mini_cc.harness.diagnostics import QueryDiagnostics
from mini_cc.harness.dispatch_roles import role_for_step
from mini_cc.harness.models import FailureClass, RunState, Step, StepKind, StepResult, TraceSpan, WorkItem
from mini_cc.harness.normalization import DEFAULT_WORK_ITEM_METADATA
from mini_cc.models import (
    AgentCompletionEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.runtime.facade import RuntimeFacade
from mini_cc.tools.base import BaseTool

StepHandler = Callable[[Step, RunState], Awaitable[StepResult]]
QueryEventSink = Callable[[Event, Step, RunState], Awaitable[None] | None]


class StepRunner:
    def __init__(
        self,
        *,
        runtime: RuntimeFacade | None = None,
        handlers: dict[StepKind, StepHandler] | None = None,
        bash_tool: BaseTool | None = None,
        query_event_sink: QueryEventSink | None = None,
    ) -> None:
        self._runtime = runtime
        self._handlers = handlers or {}
        self._bash_tool = bash_tool
        self._query_event_sink = query_event_sink
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._interrupt_event: threading.Event | None = None
        self._diag: QueryDiagnostics | None = None

    def register_handler(self, kind: StepKind, handler: StepHandler) -> None:
        self._handlers[kind] = handler

    async def run_step(self, step: Step, run_state: RunState) -> StepResult:
        timeout_seconds = self._step_timeout_seconds(step, run_state)
        self._diag = None
        scope = self._execution_scope_or_null(step, run_state)
        with scope:
            try:
                return await asyncio.wait_for(self._run_step_inner(step, run_state), timeout=timeout_seconds)
            except TimeoutError:
                return self._build_timeout_result(timeout_seconds)

    async def run_work_item(self, step: Step, work_item: WorkItem, run_state: RunState) -> StepResult:
        timeout_seconds = work_item.budget_seconds or self._step_timeout_seconds(step, run_state)
        self._diag = None
        scope = self._work_item_scope_or_null(step, work_item, run_state)
        with scope:
            try:
                return await asyncio.wait_for(
                    self._run_work_item_inner(step, work_item, run_state),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                return self._build_timeout_result(timeout_seconds)

    async def start_readonly_work_item_background(
        self, step: Step, work_item: WorkItem, run_state: RunState
    ) -> StepResult:
        if self._runtime is None or not self._runtime.has_agent_runtime:
            result = await self.run_work_item(step, work_item, run_state)
            result.metadata["dispatch_mode"] = "sync_fallback"
            return result

        base_prompt = str(work_item.inputs.get("prompt", work_item.goal))
        prior_context = self._build_prior_work_item_context(step, work_item)
        prompt = (prior_context + base_prompt) if prior_context else base_prompt

        self._inject_agent_budget(run_state)
        scope = self._work_item_scope_or_null(step, work_item, run_state)
        with scope:
            try:
                background = await self._runtime.start_background_agent(
                    prompt=prompt,
                    readonly=True,
                    fork=False,
                    parent_state=run_state.latest_query_state,
                    mode="plan",
                    scope_paths=[],
                    run_id=run_state.run_id,
                    step_id=step.id,
                    work_item_id=work_item.id,
                    role=work_item.role,
                )
            except Exception as err:
                return StepResult(
                    success=False,
                    summary="",
                    retryable=True,
                    error=str(err),
                    failure_class=self._classify_exception(err),
                )
        self._background_tasks.add(background.task)
        background.task.add_done_callback(self._background_tasks.discard)
        metadata: dict[str, str] = {
            "agent_id": background.agent_id,
            "work_item_id": work_item.id,
            "work_item_kind": work_item.kind,
            "dispatch_mode": "background",
        }
        metadata.update(self._read_back_budget_metadata())
        return StepResult(
            success=True,
            summary=f"Readonly agent {background.agent_id} started for work item {work_item.id}",
            progress_made=True,
            trace_spans=[
                TraceSpan(
                    span_id=f"{step.id}-{work_item.id}-bg",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    work_item_id=work_item.id,
                    kind="work_item",
                    name=work_item.kind,
                    status="started",
                    end_at=datetime.now(UTC).isoformat(),
                    summary=f"Background agent {background.agent_id}",
                    metadata={"mode": "plan", "readonly": "true", "dispatch_mode": "background"},
                )
            ],
            metadata=metadata,
        )

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
        if work_item.metadata.get(DEFAULT_WORK_ITEM_METADATA) == "true":
            result = await self._run_step_inner(step, run_state)
            result.metadata.setdefault("work_item_id", work_item.id)
            result.metadata.setdefault("work_item_kind", work_item.kind)
            result.metadata.setdefault(DEFAULT_WORK_ITEM_METADATA, "true")
            return result

        readonly = work_item.role in {"analyzer", "planner", "reporter", "verifier"}
        mode = "plan" if readonly else "build"
        parent_state = run_state.latest_query_state if readonly else None
        base_prompt = str(work_item.inputs.get("prompt", work_item.goal))
        prior_context = self._build_prior_work_item_context(step, work_item)
        prompt = (prior_context + base_prompt) if prior_context else base_prompt

        if self._runtime is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="RuntimeFacade is required for work-item execution",
            )
        if not self._runtime.has_agent_runtime:
            result = await self._run_query_prompt(prompt, mode, step, run_state)
            result.metadata["work_item_id"] = work_item.id
            result.metadata["work_item_kind"] = work_item.kind
            return result

        agent_run = await self._runtime.run_agent(
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

        return await self._execute_agent_loop(
            agent_id=agent_run.agent_id,
            task_id=agent_run.task_id,
            agent_events=agent_run.events,
            prompt=prompt,
            readonly=readonly,
            mode=mode,
            step=step,
            run_state=run_state,
            work_item_id=work_item.id,
            extra_metadata={"work_item_id": work_item.id, "work_item_kind": work_item.kind},
            success_label=f"Work item {work_item.id}",
        )

    async def _run_query_step(self, step: Step, run_state: RunState) -> StepResult:
        if self._runtime is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="RuntimeFacade is required for query-backed steps",
            )

        prompt = str(step.inputs.get("prompt", step.goal))
        mode = self._runtime.mode
        return await self._run_query_prompt(prompt, mode, step, run_state)

    async def _run_query_prompt(self, prompt: str, mode: str, step: Step, run_state: RunState) -> StepResult:
        if self._runtime is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="RuntimeFacade is required for query-backed steps",
            )
        state = self._runtime.prepare_query_state(run_state.latest_query_state, mode)
        text_parts: list[str] = []
        tool_outputs: list[str] = []

        diag = QueryDiagnostics(
            message_count=len(state.messages),
            turn_count=state.turn_count,
        )
        diag.started_at = time.monotonic()
        self._diag = diag

        try:
            async for event in self._runtime.submit_message(prompt, state):
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
        if self._bash_tool is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="Bash tool is not configured for this runner",
            )
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
        if self._runtime is None or not self._runtime.has_agent_runtime:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="AgentManager is required for readonly agent steps",
            )

        prompt = str(step.inputs.get("prompt", step.goal))
        try:
            background = await self._runtime.start_background_agent(
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
        except Exception as err:
            return StepResult(
                success=False,
                summary="",
                retryable=True,
                error=str(err),
                failure_class=self._classify_exception(err),
            )
        self._background_tasks.add(background.task)
        background.task.add_done_callback(self._background_tasks.discard)
        metadata: dict[str, str] = {"agent_id": background.agent_id}
        metadata.update(self._read_back_budget_metadata())
        return StepResult(
            success=True,
            summary=f"Readonly agent {background.agent_id} started for step {step.id}",
            progress_made=True,
            trace_spans=[
                TraceSpan(
                    span_id=f"{step.id}-readonly-agent",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    work_item_id=step.id,
                    kind="agent",
                    name=background.agent_id,
                    status="started",
                    end_at=datetime.now(UTC).isoformat(),
                    metadata={"mode": "plan", "readonly": "true"},
                )
            ],
            metadata=metadata,
        )

    async def _run_delegated_agent_step(self, step: Step, run_state: RunState) -> StepResult | None:
        if self._runtime is None or not self._runtime.has_agent_runtime:
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

        agent_run = await self._runtime.run_agent(
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

        return await self._execute_agent_loop(
            agent_id=agent_run.agent_id,
            task_id=agent_run.task_id,
            agent_events=agent_run.events,
            prompt=prompt,
            readonly=readonly,
            mode=mode,
            step=step,
            run_state=run_state,
            work_item_id=step.id,
            extra_metadata={},
            success_label=f"Delegated step {step.id}",
        )

    async def _execute_agent_loop(
        self,
        *,
        agent_id: str,
        task_id: int,
        agent_events: Any,
        prompt: str,
        readonly: bool,
        mode: str,
        step: Step,
        run_state: RunState,
        work_item_id: str,
        extra_metadata: dict[str, str],
        success_label: str,
    ) -> StepResult:
        parent_state = run_state.latest_query_state if readonly else None
        diag = QueryDiagnostics(
            message_count=(len(parent_state.messages) if parent_state is not None else 0),
            turn_count=(parent_state.turn_count if parent_state is not None else 0),
        )
        diag.started_at = time.monotonic()
        self._diag = diag

        start_event = AgentStartEvent(agent_id=agent_id, task_id=task_id, prompt=prompt[:80])
        diag.record_event(start_event)
        await self._emit_query_event(start_event, step, run_state)

        text_parts: list[str] = []
        try:
            async for event in agent_events:
                if isinstance(event, TextDelta):
                    text_parts.append(event.content)
                    continue
                if isinstance(event, ToolCallStart):
                    call_event = AgentToolCallEvent(agent_id=agent_id, tool_name=event.name)
                    diag.record_event(call_event)
                    await self._emit_query_event(call_event, step, run_state)
                    continue
                if isinstance(event, ToolResultEvent):
                    preview = event.output[:100] + ("..." if len(event.output) > 100 else "")
                    result_event = AgentToolResultEvent(
                        agent_id=agent_id,
                        tool_name=event.name,
                        success=event.success,
                        output_preview=preview,
                    )
                    diag.record_event(result_event)
                    await self._emit_query_event(result_event, step, run_state)
        except Exception as err:
            completion = self._drain_completion_for_agent(agent_id)
            if completion is not None:
                diag.record_event(completion)
                await self._emit_query_event(completion, step, run_state)
            diag.finish_turn()
            diag.error_type = type(err).__name__
            diag.error_detail = str(err)
            metadata = diag.to_metadata()
            metadata.update(self._read_back_budget_metadata())
            metadata["delegated_agent_id"] = agent_id
            metadata["delegated_agent_mode"] = mode
            metadata["delegated_agent_readonly"] = str(readonly).lower()
            metadata.update(extra_metadata)
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
                    work_item_id=work_item_id,
                ),
                metadata=metadata,
            )

        completion = self._drain_completion_for_agent(agent_id)
        if completion is not None:
            diag.record_event(completion)
            await self._emit_query_event(completion, step, run_state)
        diag.finish_turn()
        summary = "".join(text_parts).strip()
        if not summary and completion is not None:
            summary = completion.output.strip()
        metadata = diag.to_metadata()
        metadata.update(self._read_back_budget_metadata())
        metadata["delegated_agent_id"] = agent_id
        metadata["delegated_agent_mode"] = mode
        metadata["delegated_agent_readonly"] = str(readonly).lower()
        metadata.update(extra_metadata)
        return StepResult(
            success=True,
            summary=summary or f"{success_label} completed via agent {agent_id}",
            progress_made=bool(summary or completion is not None),
            trace_spans=diag.build_trace_spans(
                run_id=run_state.run_id,
                step_id=step.id,
                work_item_id=work_item_id,
            ),
            metadata=metadata,
        )

    def _build_prior_work_item_context(self, step: Step, work_item: WorkItem) -> str:
        if not work_item.depends_on:
            return ""
        dep_parts: list[str] = []
        for dep_id in work_item.depends_on:
            for item in step.work_items:
                if item.id == dep_id and item.summary:
                    dep_parts.append(f"### 前置任务「{item.title}」的结果\n\n{item.summary}")
                    break
        if not dep_parts:
            return ""
        return (
            "以下是前序任务已完成的分析结果，请直接基于这些结果工作，不要重复已有分析：\n\n"
            + "\n\n".join(dep_parts)
            + "\n\n---\n\n"
        )

    def _drain_completion_for_agent(self, agent_id: str) -> AgentCompletionEvent | None:
        if self._runtime is None:
            return None
        return self._runtime.drain_completion(agent_id)

    async def _emit_query_event(self, event: Event, step: Step, run_state: RunState) -> None:
        if self._query_event_sink is None:
            return
        result = self._query_event_sink(event, step, run_state)
        if inspect.isawaitable(result):
            await result

    def _execution_scope_or_null(self, step: Step, run_state: RunState) -> AbstractContextManager[None]:
        if self._runtime is None:
            return nullcontext()
        self._inject_agent_budget(run_state)
        return self._runtime.execution_scope(
            run_id=run_state.run_id,
            mode="build" if step.kind in {StepKind.BOOTSTRAP_PROJECT, StepKind.EDIT_CODE} else "plan",
            agent_budget=self._runtime.agent_budget,
            interrupt_event=self._interrupt_event,
        )

    def _work_item_scope_or_null(
        self, step: Step, work_item: WorkItem, run_state: RunState
    ) -> AbstractContextManager[None]:
        if self._runtime is None:
            return nullcontext()
        self._inject_agent_budget(run_state)
        mode = "build" if work_item.role == "implementer" else "plan"
        return self._runtime.execution_scope(
            run_id=run_state.run_id,
            mode=mode,
            agent_budget=self._runtime.agent_budget,
            interrupt_event=self._interrupt_event,
        )

    def _inject_agent_budget(self, run_state: RunState) -> None:
        if self._runtime is None:
            return
        from mini_cc.harness.models import AgentBudget

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
        self._runtime.agent_budget = AgentBudget(
            max_readonly=configured.max_readonly,
            max_write=1,
            remaining_readonly=max(0, configured.max_readonly - active_readonly),
            remaining_write=max(0, 1 - active_write),
        )

    def _read_back_budget_metadata(self) -> dict[str, str]:
        if self._runtime is None:
            return {}
        budget = self._runtime.agent_budget
        if budget is None:
            return {}
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

    def set_step_context(self, step: Step) -> None:
        if self._runtime is not None:
            self._runtime.set_step_context(step.id)

    def clear_step_context(self) -> None:
        if self._runtime is not None:
            self._runtime.set_step_context(None)

    def set_interrupt_event(self, interrupt_event: threading.Event | None) -> None:
        self._interrupt_event = interrupt_event

    def cancel_active_agents(self, agent_ids: list[str] | None = None) -> list[str]:
        if self._runtime is None:
            return []
        return self._runtime.cancel_agents(agent_ids)

    def _is_interrupted(self) -> bool:
        return self._interrupt_event.is_set() if self._interrupt_event is not None else False

from __future__ import annotations

import asyncio
import inspect
import secrets
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.doc_generator import RunDocGenerator
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.iteration import IterationOptimizer, IterationSnapshot
from mini_cc.harness.judge import RunJudge
from mini_cc.harness.models import (
    AgentTrace,
    RunState,
    RunStatus,
    SchedulerDecisionRecord,
    Step,
    StepKind,
    StepResult,
    StepStatus,
    TraceSpan,
    WorkItem,
    WorkItemStatus,
    deadline_after,
    utc_now_iso,
)
from mini_cc.harness.policy import PolicyAction, PolicyDecision, PolicyEngine
from mini_cc.harness.scheduler import Scheduler
from mini_cc.harness.step_runner import StepRunner

HarnessEventSink = Callable[[HarnessEvent, RunState], Awaitable[None] | None]


class SupervisorLoop:
    def __init__(
        self,
        *,
        store: CheckpointStore,
        step_runner: StepRunner,
        policy_engine: PolicyEngine | None = None,
        judge: RunJudge | None = None,
        iteration_optimizer: IterationOptimizer | None = None,
        doc_generator: RunDocGenerator | None = None,
        scheduler: Scheduler | None = None,
        event_sink: HarnessEventSink | None = None,
        drain_lifecycle: Callable[[], list[Any]] | None = None,
    ) -> None:
        self._store = store
        self._step_runner = step_runner
        self._policy = policy_engine or PolicyEngine()
        self._judge = judge or RunJudge()
        self._iteration = iteration_optimizer or IterationOptimizer()
        self._doc_generator = doc_generator or RunDocGenerator()
        self._scheduler = scheduler or Scheduler()
        self._event_sink = event_sink
        self._drain_lifecycle = drain_lifecycle

    async def run(self, run_state: RunState) -> RunState:
        return await self.run_with_interrupt(run_state, interrupt_event=None)

    async def run_with_interrupt(
        self,
        run_state: RunState,
        *,
        interrupt_event: threading.Event | None,
    ) -> RunState:
        if run_state.started_at is None:
            run_state.started_at = run_state.created_at
        if run_state.status == RunStatus.CREATED:
            run_state.status = RunStatus.RUNNING
            run_state.phase = "running"
            await self._emit_event(
                HarnessEvent(
                    event_type="run_started",
                    run_id=run_state.run_id,
                    message=run_state.goal,
                ),
                run_state,
            )
            self._store.save_state(run_state)

        while not run_state.is_terminal:
            if interrupt_event is not None and interrupt_event.is_set():
                run_state.status = RunStatus.CANCELLED
                run_state.phase = "cancelled"
                run_state.current_step_id = None
                run_state.touch()
                self._store.save_state(run_state)
                break
            if self._cooldown_active(run_state):
                run_state.status = RunStatus.COOLDOWN
                run_state.phase = "cooldown"
                run_state.touch()
                self._store.save_state(run_state)
                await asyncio.sleep(0.1)
                continue
            if run_state.status == RunStatus.COOLDOWN:
                run_state.status = RunStatus.RUNNING
                run_state.phase = "running"
                run_state.cooldown_until = None
                run_state.cooldown_reason = None
            self._drain_and_update_agents(run_state)
            await asyncio.sleep(0)
            limit_decision = self._policy.check_run_limits(run_state)
            if limit_decision is not None:
                self._apply_terminal_decision(run_state, limit_decision)
                break

            scheduling_decision = self._scheduler.decide(run_state)
            if scheduling_decision is None:
                if run_state.active_agent_count > 0:
                    await asyncio.sleep(0.05)
                    continue
                run_state.status = RunStatus.COMPLETED
                run_state.phase = "completed"
                run_state.current_step_id = None
                await self._emit_event(
                    HarnessEvent(
                        event_type="run_completed",
                        run_id=run_state.run_id,
                        message="No pending steps remain",
                    ),
                    run_state,
                )
                self._store.save_state(run_state)
                break
            step = scheduling_decision.selected.step
            work_item = scheduling_decision.selected.work_item

            step.status = StepStatus.IN_PROGRESS
            run_state.current_step_id = step.id
            run_state.current_work_item_id = work_item.id if work_item is not None else None
            execution_started_at = utc_now_iso()
            if work_item is not None:
                work_item.status = WorkItemStatus.IN_PROGRESS
                step.sync_work_item(work_item)
                run_state.phase = work_item.kind
            else:
                run_state.phase = step.kind.value
            run_state.sync_step(step)
            run_state.touch()
            self._store.append_scheduler_decision(
                SchedulerDecisionRecord(
                    run_id=run_state.run_id,
                    step_id=step.id,
                    work_item_id=work_item.id if work_item is not None else None,
                    selected_role=scheduling_decision.selected.role,
                    selected_priority=scheduling_decision.selected.priority,
                    considered_count=scheduling_decision.considered_count,
                    reason=scheduling_decision.reason,
                    rejected_targets=[
                        rejected.work_item.id if rejected.work_item is not None else rejected.step.id
                        for rejected in scheduling_decision.rejected
                    ],
                    rejected_reasons=[rejected.reason for rejected in scheduling_decision.rejected],
                )
            )
            await self._emit_event(
                HarnessEvent(
                    event_type="step_started",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    message=work_item.title if work_item is not None else step.title,
                    data={
                        "kind": step.kind.value,
                        "work_item_id": work_item.id if work_item is not None else "",
                        "work_item_kind": work_item.kind if work_item is not None else "",
                        "active_agents": str(run_state.active_agent_count),
                        "failure_count": str(run_state.failure_count),
                        "no_progress_count": str(run_state.consecutive_no_progress_count),
                        "replan_count": str(run_state.replan_count),
                        "scheduler_reason": scheduling_decision.reason,
                        "scheduler_considered": str(scheduling_decision.considered_count),
                        "scheduler_rejected": ",".join(
                            (rejected.work_item.id if rejected.work_item is not None else rejected.step.id)
                            for rejected in scheduling_decision.rejected[:3]
                        ),
                    },
                ),
                run_state,
            )
            self._store.save_state(run_state)

            self._set_step_context(step)
            self._step_runner.set_interrupt_event(interrupt_event)
            try:
                if work_item is not None:
                    result = await self._step_runner.run_work_item(step, work_item, run_state)
                elif step.has_work_items and step.has_terminal_work_item_failure():
                    result = self._failed_work_item_result(step)
                else:
                    result = await self._step_runner.run_step(step, run_state)
            except Exception as err:
                result = StepResult(
                    success=False,
                    summary="",
                    retryable=True,
                    error=f"Unhandled step exception: {err}",
                    progress_made=False,
                )
            finally:
                self._step_runner.set_interrupt_event(None)
                self._clear_step_context()
            if interrupt_event is not None and interrupt_event.is_set():
                self._cancel_step(run_state, step, work_item, result)
                break
            self._drain_and_update_agents(run_state)

            await self._apply_step_result(run_state, step, work_item, result, execution_started_at)

            if run_state.is_terminal:
                break

        self._finalize_terminal_run(run_state)
        return run_state

    def _cancel_step(
        self,
        run_state: RunState,
        step: Step,
        work_item: WorkItem | None,
        result: StepResult,
    ) -> None:
        step.error = result.error or "run cancelled during step execution"
        step.status = StepStatus.FAILED_TERMINAL
        if work_item is not None:
            work_item.error = step.error
            work_item.status = WorkItemStatus.FAILED_TERMINAL
            step.sync_work_item(work_item)
        run_state.sync_step(step)
        run_state.status = RunStatus.CANCELLED
        run_state.phase = "cancelled"
        run_state.current_step_id = None
        run_state.current_work_item_id = None
        run_state.touch()
        self._store.save_state(run_state)

    async def _apply_step_result(
        self,
        run_state: RunState,
        step: Step,
        work_item: WorkItem | None,
        result: StepResult,
        execution_started_at: str,
    ) -> None:
        artifact_owner = work_item.id if work_item is not None else step.id
        artifact_paths = self._save_artifacts(run_state, step, result.artifacts, artifact_owner=artifact_owner)
        previous_snapshot = self._store.latest_iteration_snapshot(run_state.run_id)
        if work_item is not None:
            work_item.summary = result.summary
            work_item.error = result.error
            work_item.artifacts.update(artifact_paths)
            work_item.metadata.update(result.metadata)
            step.sync_work_item(work_item)
            step.summary = self._summarize_step_work_items(step)
            step.error = result.error
            step.artifacts.update(artifact_paths)
        else:
            step.summary = result.summary
            step.evaluation = result.summary
            step.error = result.error
            step.artifacts.update(artifact_paths)
        run_state.artifacts.update(artifact_paths)
        run_state.latest_summary = result.summary
        if result.query_state is not None:
            run_state.latest_query_state = result.query_state

        self._update_step_counters(run_state, step)
        self._update_work_item_status(run_state, step, work_item, result)
        self._update_failure_tracking(run_state, result)

        if work_item is not None and result.success and step.pending_work_items():
            self._append_trace_spans(
                run_state,
                result.trace_spans,
                self._execution_span(
                    run_state=run_state,
                    step=step,
                    work_item=work_item,
                    started_at=execution_started_at,
                    status="success",
                    summary=result.summary,
                ),
            )
            run_state.sync_step(step)
            run_state.current_step_id = None
            run_state.current_work_item_id = None
            run_state.status = RunStatus.RUNNING
            run_state.touch()
            await self._emit_event(
                HarnessEvent(
                    event_type="step_completed",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    message=result.summary[:200],
                    data={
                        "success": "true",
                        "health": "",
                        "decision": "continue_work_item",
                        "decision_reason": "work item succeeded; continue remaining work items",
                        "work_item_id": work_item.id,
                        "work_item_kind": work_item.kind,
                        "active_agents": str(run_state.active_agent_count),
                        "failure_count": str(run_state.failure_count),
                        "no_progress_count": str(run_state.consecutive_no_progress_count),
                        "replan_count": str(run_state.replan_count),
                        "failure_class": "",
                        "inserted_steps": "",
                        **self._result_metadata(result),
                    },
                ),
                run_state,
            )
            self._store.save_state(run_state)
            self._store.save_checkpoint(run_state, f"step-{step.id}")
            return

        await self._finalize_step_with_review(
            run_state, step, work_item, result, execution_started_at, artifact_paths, previous_snapshot
        )

    async def _finalize_step_with_review(
        self,
        run_state: RunState,
        step: Step,
        work_item: WorkItem | None,
        result: StepResult,
        execution_started_at: str,
        artifact_paths: dict[str, str],
        previous_snapshot: IterationSnapshot | None,
    ) -> None:
        final_result = result if work_item is None else self._finalize_step_result(step, result)
        self._append_trace_spans(
            run_state,
            final_result.trace_spans,
            self._execution_span(
                run_state=run_state,
                step=step,
                work_item=work_item,
                started_at=execution_started_at,
                status="success" if final_result.success else "failed",
                summary=final_result.summary or (final_result.error or ""),
            ),
        )
        snapshot = self._iteration.capture(run_state, step, final_result, artifact_paths)
        review = self._iteration.review(snapshot, previous_snapshot)
        health = self._judge.assess(run_state, step, final_result)
        decision = self._policy.evaluate_step(run_state, step, final_result, health)
        generated_steps = self._iteration.apply_review(run_state, step, final_result, review)
        self._store.append_iteration_snapshot(snapshot)
        self._store.append_iteration_review(review)
        self._store.append_journal_entry(
            run_state.run_id,
            self._iteration.format_journal_entry(snapshot, review, generated_steps + decision.insert_steps),
        )
        self._apply_step_decision(run_state, step, final_result, decision)

        next_steps = list(final_result.next_steps)
        next_steps.extend(generated_steps)
        next_steps.extend(decision.insert_steps)
        if next_steps:
            next_steps = self._iteration.apply_constraints_to_steps(next_steps, review)
            self._append_generated_steps(run_state, next_steps)

        run_state.current_step_id = None
        run_state.current_work_item_id = None
        if run_state.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.TIMED_OUT}:
            run_state.status = RunStatus.RUNNING
        run_state.touch()
        await self._emit_event(
            HarnessEvent(
                event_type="step_completed",
                run_id=run_state.run_id,
                step_id=step.id,
                message=final_result.summary[:200],
                data={
                    "success": str(final_result.success).lower(),
                    "health": health.value,
                    "decision": decision.action.value,
                    "decision_reason": decision.reason,
                    "work_item_id": work_item.id if work_item is not None else "",
                    "work_item_kind": work_item.kind if work_item is not None else "",
                    "active_agents": str(run_state.active_agent_count),
                    "failure_count": str(run_state.failure_count),
                    "no_progress_count": str(run_state.consecutive_no_progress_count),
                    "replan_count": str(run_state.replan_count),
                    "failure_class": (
                        final_result.failure_class.value if final_result.failure_class is not None else ""
                    ),
                    "inserted_steps": ",".join(next_step.kind.value for next_step in next_steps),
                    **self._result_metadata(final_result),
                },
            ),
            run_state,
        )
        self._store.save_state(run_state)
        self._store.save_checkpoint(run_state, f"step-{step.id}")

    def _update_step_counters(self, run_state: RunState, step: Step) -> None:
        if step.kind == StepKind.RUN_TESTS:
            run_state.test_run_count += 1
            run_state.bash_command_count += 1
            run_state.status = RunStatus.VERIFYING
        elif step.kind == StepKind.INSPECT_FAILURES:
            run_state.bash_command_count += 1
            run_state.status = RunStatus.RUNNING
        elif step.kind == StepKind.RUN_TASK_AUDIT:
            run_state.bash_command_count += 1
        elif step.kind == StepKind.EDIT_CODE:
            run_state.status = RunStatus.RUNNING

    def _update_work_item_status(
        self,
        run_state: RunState,
        step: Step,
        work_item: WorkItem | None,
        result: StepResult,
    ) -> None:
        if work_item is None:
            return
        if result.success:
            work_item.status = WorkItemStatus.SUCCEEDED
        elif result.retryable and work_item.retry_count < run_state.retry_policy.max_step_retries:
            work_item.status = WorkItemStatus.PENDING
            work_item.retry_count += 1
        elif not result.success:
            work_item.status = WorkItemStatus.FAILED_TERMINAL
        step.sync_work_item(work_item)

    def _update_failure_tracking(self, run_state: RunState, result: StepResult) -> None:
        if result.success:
            run_state.failure_count = 0
            run_state.provider_cooldown_count = 0
            run_state.consecutive_no_progress_count = (
                0 if result.progress_made else (run_state.consecutive_no_progress_count + 1)
            )
        elif result.failure_class is not None and result.failure_class.value == "transient_provider":
            run_state.provider_cooldown_count += 1
        else:
            run_state.failure_count += 1
            run_state.consecutive_no_progress_count += 0 if result.progress_made else 1

    def _save_artifacts(
        self, run_state: RunState, step: Step, artifacts: dict[str, str], *, artifact_owner: str | None = None
    ) -> dict[str, str]:
        saved: dict[str, str] = {}
        for name, content in artifacts.items():
            owner = artifact_owner or step.id
            path = self._store.save_artifact(run_state.run_id, f"{owner}_{name}", content)
            saved[name] = path
        return saved

    def _result_metadata(self, result: StepResult) -> dict[str, str]:
        metadata = dict(result.metadata)
        if not result.trace_spans:
            return metadata
        metadata.setdefault("trace_span_count", str(len(result.trace_spans)))
        metadata.setdefault("trace_tool_count", str(sum(1 for span in result.trace_spans if span.kind == "tool")))
        metadata.setdefault("trace_agent_count", str(sum(1 for span in result.trace_spans if span.kind == "agent")))
        metadata.setdefault(
            "trace_work_item_count",
            str(sum(1 for span in result.trace_spans if span.kind == "work_item")),
        )
        metadata.setdefault("trace_outline", self._trace_outline(result.trace_spans))
        return metadata

    def _trace_outline(self, spans: list[TraceSpan]) -> str:
        parts: list[str] = []
        for span in spans[:8]:
            parts.append(f"{span.kind}:{span.name}[{span.status}]")
        return " -> ".join(parts)

    def _finalize_step_result(self, step: Step, result: StepResult) -> StepResult:
        summary = self._summarize_step_work_items(step)
        if step.all_work_items_succeeded():
            return StepResult(
                success=True,
                summary=summary or result.summary,
                artifacts=result.artifacts,
                next_steps=result.next_steps,
                retryable=result.retryable,
                progress_made=bool(summary),
                query_state=result.query_state,
                trace_spans=result.trace_spans,
                metadata=result.metadata,
            )
        return StepResult(
            success=False,
            summary=summary,
            artifacts=result.artifacts,
            next_steps=result.next_steps,
            retryable=result.retryable,
            error=result.error,
            timed_out=result.timed_out,
            progress_made=result.progress_made,
            query_state=result.query_state,
            failure_class=result.failure_class,
            trace_spans=result.trace_spans,
            metadata=result.metadata,
        )

    def _summarize_step_work_items(self, step: Step) -> str:
        parts = [item.summary.strip() for item in step.work_items if item.summary.strip()]
        return "\n\n".join(parts[:6])

    def _append_trace_spans(self, run_state: RunState, spans: list[TraceSpan], root_span: TraceSpan) -> None:
        self._store.append_trace_span(root_span)
        for span in spans:
            to_store = (
                span
                if span.parent_span_id is not None
                else span.model_copy(update={"parent_span_id": root_span.span_id})
            )
            self._store.append_trace_span(to_store)

    def _execution_span(
        self,
        *,
        run_state: RunState,
        step: Step,
        work_item: WorkItem | None,
        started_at: str,
        status: str,
        summary: str,
    ) -> TraceSpan:
        ended_at = utc_now_iso()
        start_dt = datetime.fromisoformat(started_at)
        end_dt = datetime.fromisoformat(ended_at)
        return TraceSpan(
            span_id=f"{step.id}-{work_item.id if work_item is not None else 'step'}",
            run_id=run_state.run_id,
            step_id=step.id,
            work_item_id=work_item.id if work_item is not None else None,
            kind="work_item" if work_item is not None else "step",
            name=work_item.kind if work_item is not None else step.kind.value,
            status=status,
            start_at=started_at,
            end_at=ended_at,
            duration_ms=max(0, int((end_dt - start_dt).total_seconds() * 1000)),
            summary=summary[:200],
        )

    def _failed_work_item_result(self, step: Step) -> StepResult:
        failed = next(
            item
            for item in step.work_items
            if item.status in {WorkItemStatus.FAILED_TERMINAL, WorkItemStatus.FAILED_RETRYABLE}
        )
        return StepResult(
            success=False,
            summary=self._summarize_step_work_items(step),
            retryable=False,
            error=failed.error,
            progress_made=bool(failed.summary.strip()),
            metadata=failed.metadata,
        )

    def _apply_step_decision(
        self,
        run_state: RunState,
        step: Step,
        result: StepResult,
        decision: PolicyDecision,
    ) -> None:
        if decision.action == PolicyAction.CONTINUE:
            step.status = StepStatus.SUCCEEDED
            if step.id not in run_state.completed_step_ids:
                run_state.completed_step_ids.append(step.id)
        elif decision.action == PolicyAction.RETRY:
            step.status = StepStatus.PENDING
            step.retry_count += 1
        elif decision.action == PolicyAction.COOLDOWN:
            step.status = StepStatus.PENDING
            step.retry_count += 1
            run_state.status = RunStatus.COOLDOWN
            run_state.phase = "cooldown"
            if decision.cooldown_seconds is not None:
                run_state.cooldown_until = deadline_after(decision.cooldown_seconds)
            run_state.cooldown_reason = decision.reason
        elif decision.action == PolicyAction.REPLAN:
            run_state.replan_count += 1
            step.status = StepStatus.FAILED_RETRYABLE if not result.success else StepStatus.SUCCEEDED
            if result.success and step.id not in run_state.completed_step_ids:
                run_state.completed_step_ids.append(step.id)
            if not result.success and step.id not in run_state.failed_step_ids:
                run_state.failed_step_ids.append(step.id)
        elif decision.action == PolicyAction.COMPLETE:
            step.status = StepStatus.SUCCEEDED
            if step.id not in run_state.completed_step_ids:
                run_state.completed_step_ids.append(step.id)
            run_state.status = RunStatus.COMPLETED
            run_state.phase = "completed"
        elif decision.action == PolicyAction.BLOCK:
            step.status = StepStatus.FAILED_TERMINAL
            if step.id not in run_state.failed_step_ids:
                run_state.failed_step_ids.append(step.id)
            run_state.status = RunStatus.BLOCKED
            run_state.phase = "blocked"
        elif decision.action == PolicyAction.FAIL:
            step.status = StepStatus.FAILED_TERMINAL
            if step.id not in run_state.failed_step_ids:
                run_state.failed_step_ids.append(step.id)
            run_state.status = decision.terminal_status or RunStatus.FAILED
            run_state.phase = run_state.status.value
        elif decision.action == PolicyAction.TIME_OUT:
            run_state.status = RunStatus.TIMED_OUT
            run_state.phase = "timed_out"

        run_state.sync_step(step)

    def _apply_terminal_decision(self, run_state: RunState, decision: PolicyDecision) -> None:
        if decision.terminal_status is not None:
            run_state.status = decision.terminal_status
        if decision.action == PolicyAction.TIME_OUT:
            run_state.phase = "timed_out"
            event_type = "run_timed_out"
        else:
            run_state.phase = run_state.status.value
            event_type = "run_failed"
        run_state.touch()
        self._store.append_event(
            HarnessEvent(
                event_type=event_type,
                run_id=run_state.run_id,
                message=decision.reason,
                data={
                    "decision": decision.action.value,
                    "decision_reason": decision.reason,
                    "active_agents": str(run_state.active_agent_count),
                    "failure_count": str(run_state.failure_count),
                    "no_progress_count": str(run_state.consecutive_no_progress_count),
                    "replan_count": str(run_state.replan_count),
                },
            )
        )
        self._store.save_state(run_state)

    def _cooldown_active(self, run_state: RunState) -> bool:
        if run_state.cooldown_until is None:
            return False
        return datetime.fromisoformat(run_state.cooldown_until) > datetime.now(UTC)

    async def _emit_event(self, event: HarnessEvent, run_state: RunState) -> None:
        self._store.append_event(event)
        if self._event_sink is None:
            return
        result = self._event_sink(event, run_state)
        if inspect.isawaitable(result):
            await result

    def _append_generated_steps(self, run_state: RunState, steps: list[Step]) -> None:
        insert_at = len(run_state.steps)
        current_step = run_state.current_step_id
        if current_step is not None:
            for index, step in enumerate(run_state.steps):
                if step.id == current_step:
                    insert_at = index + 1
                    break

        normalized: list[Step] = []
        for step in steps:
            if not step.id:
                step.id = f"step-{secrets.token_hex(4)}"
            normalized.append(step)
        run_state.steps[insert_at:insert_at] = normalized

    def _set_step_context(self, step: Step) -> None:
        self._step_runner.set_step_context(step)

    def _clear_step_context(self) -> None:
        self._step_runner.clear_step_context()

    def _drain_and_update_agents(self, run_state: RunState) -> None:
        if self._drain_lifecycle is None:
            return
        events = self._drain_lifecycle()
        self._update_spawned_agents(run_state, events)

    def _update_spawned_agents(self, run_state: RunState, events: list[Any]) -> None:
        for event in events:
            if event.event_type == "created":
                trace = AgentTrace(
                    agent_id=event.agent_id,
                    source_step_id=event.source_step_id,
                    readonly=event.readonly,
                    scope_paths=event.scope_paths or [],
                )
                run_state.spawned_agents.append(trace)
            elif event.event_type in {"completed", "cancelled"}:
                for trace in run_state.spawned_agents:
                    if trace.agent_id == event.agent_id and trace.completed_at is None:
                        trace.completed_at = utc_now_iso()
                        trace.success = event.success
                        trace.output_preview = event.output_preview
                        trace.output_path = event.output_path
                        trace.is_stale = event.is_stale
                        trace.base_version_stamp = event.base_version_stamp
                        trace.completed_version_stamp = event.completed_version_stamp
                        trace.termination_reason = event.termination_reason
                        break
        self._refresh_agent_metrics(run_state)

    def _finalize_terminal_run(self, run_state: RunState) -> None:
        if not run_state.is_terminal:
            return
        self._refresh_agent_metrics(run_state)
        documentation = self._doc_generator.generate(run_state, self._store)
        path = self._store.save_documentation(run_state.run_id, documentation)
        run_state.artifacts["Documentation.md"] = str(path)
        run_state.touch()
        self._store.save_state(run_state)

    def _refresh_agent_metrics(self, run_state: RunState) -> None:
        readonly_created = sum(1 for trace in run_state.spawned_agents if trace.readonly)
        write_created = sum(1 for trace in run_state.spawned_agents if not trace.readonly)
        succeeded = sum(1 for trace in run_state.spawned_agents if trace.success is True)
        failed = sum(1 for trace in run_state.spawned_agents if trace.success is False)
        stale = sum(1 for trace in run_state.spawned_agents if trace.is_stale)
        cancelled = sum(1 for trace in run_state.spawned_agents if trace.termination_reason == "cancelled")
        peak_active = max(
            run_state.active_agent_count,
            int(run_state.metadata.get("agent_peak_active", "0")),
        )
        generic_issues: list[str] = []
        for trace in run_state.spawned_agents:
            if trace.is_stale:
                generic_issues.append(f"{trace.agent_id} returned stale results")
            elif trace.success is False:
                reason = trace.termination_reason or "unknown failure"
                generic_issues.append(f"{trace.agent_id} failed: {reason}")
        latest_issue = generic_issues[-1] if generic_issues else ""
        run_state.metadata["agents_created_readonly"] = str(readonly_created)
        run_state.metadata["agents_created_write"] = str(write_created)
        run_state.metadata["agents_succeeded"] = str(succeeded)
        run_state.metadata["agents_failed"] = str(failed)
        run_state.metadata["agents_stale"] = str(stale)
        run_state.metadata["agents_cancelled"] = str(cancelled)
        run_state.metadata["agent_peak_active"] = str(peak_active)
        if latest_issue:
            run_state.metadata["agent_latest_issue"] = latest_issue

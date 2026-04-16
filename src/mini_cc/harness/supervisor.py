from __future__ import annotations

import asyncio
import inspect
import secrets
import threading
from collections.abc import Awaitable, Callable

from mini_cc.agent.bus import AgentEventBus, AgentLifecycleEvent
from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.doc_generator import RunDocGenerator
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.iteration import IterationOptimizer
from mini_cc.harness.judge import RunJudge
from mini_cc.harness.models import (
    AgentTrace,
    RunState,
    RunStatus,
    Step,
    StepKind,
    StepResult,
    StepStatus,
    utc_now_iso,
)
from mini_cc.harness.policy import PolicyAction, PolicyDecision, PolicyEngine
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
        event_sink: HarnessEventSink | None = None,
        lifecycle_bus: AgentEventBus | None = None,
    ) -> None:
        self._store = store
        self._step_runner = step_runner
        self._policy = policy_engine or PolicyEngine()
        self._judge = judge or RunJudge()
        self._iteration = iteration_optimizer or IterationOptimizer()
        self._doc_generator = doc_generator or RunDocGenerator()
        self._event_sink = event_sink
        self._lifecycle_bus = lifecycle_bus

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
            self._drain_and_update_agents(run_state)
            await asyncio.sleep(0)
            limit_decision = self._policy.check_run_limits(run_state)
            if limit_decision is not None:
                self._apply_terminal_decision(run_state, limit_decision)
                break

            step = self._select_next_step(run_state)
            if step is None:
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

            step.status = StepStatus.IN_PROGRESS
            run_state.current_step_id = step.id
            run_state.phase = step.kind.value
            run_state.sync_step(step)
            run_state.touch()
            await self._emit_event(
                HarnessEvent(
                    event_type="step_started",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    message=step.title,
                    data={
                        "kind": step.kind.value,
                        "active_agents": str(run_state.active_agent_count),
                        "failure_count": str(run_state.failure_count),
                        "no_progress_count": str(run_state.consecutive_no_progress_count),
                        "replan_count": str(run_state.replan_count),
                    },
                ),
                run_state,
            )
            self._store.save_state(run_state)

            self._set_step_context(step)
            self._step_runner.set_interrupt_event(interrupt_event)
            try:
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
                step.error = result.error or "run cancelled during step execution"
                step.status = StepStatus.FAILED_TERMINAL
                run_state.sync_step(step)
                run_state.status = RunStatus.CANCELLED
                run_state.phase = "cancelled"
                run_state.current_step_id = None
                run_state.touch()
                self._store.save_state(run_state)
                break
            self._drain_and_update_agents(run_state)

            artifact_paths = self._save_artifacts(run_state, step, result.artifacts)
            previous_snapshot = self._store.latest_iteration_snapshot(run_state.run_id)
            snapshot = self._iteration.capture(run_state, step, result, artifact_paths)
            review = self._iteration.review(snapshot, previous_snapshot)

            step.summary = result.summary
            step.evaluation = result.summary
            step.error = result.error
            step.artifacts.update(artifact_paths)
            run_state.artifacts.update(artifact_paths)
            run_state.latest_summary = result.summary
            if result.query_state is not None:
                run_state.latest_query_state = result.query_state

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

            if result.success:
                run_state.failure_count = 0
                run_state.consecutive_no_progress_count = (
                    0 if result.progress_made else (run_state.consecutive_no_progress_count + 1)
                )
            else:
                run_state.failure_count += 1
                run_state.consecutive_no_progress_count += 0 if result.progress_made else 1

            health = self._judge.assess(run_state, step, result)
            decision = self._policy.evaluate_step(run_state, step, result, health)
            generated_steps = self._iteration.apply_review(run_state, step, result, review)
            self._store.append_iteration_snapshot(snapshot)
            self._store.append_iteration_review(review)
            self._store.append_journal_entry(
                run_state.run_id,
                self._iteration.format_journal_entry(snapshot, review, generated_steps + decision.insert_steps),
            )
            self._apply_step_decision(run_state, step, result, decision)

            next_steps = list(result.next_steps)
            next_steps.extend(generated_steps)
            next_steps.extend(decision.insert_steps)
            if next_steps:
                next_steps = self._iteration.apply_constraints_to_steps(next_steps, review)
                self._append_generated_steps(run_state, next_steps)

            run_state.current_step_id = None
            if run_state.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.TIMED_OUT}:
                run_state.status = RunStatus.RUNNING
            run_state.touch()
            await self._emit_event(
                HarnessEvent(
                    event_type="step_completed",
                    run_id=run_state.run_id,
                    step_id=step.id,
                    message=result.summary[:200],
                    data={
                        "success": str(result.success).lower(),
                        "health": health.value,
                        "decision": decision.action.value,
                        "decision_reason": decision.reason,
                        "active_agents": str(run_state.active_agent_count),
                        "failure_count": str(run_state.failure_count),
                        "no_progress_count": str(run_state.consecutive_no_progress_count),
                        "replan_count": str(run_state.replan_count),
                        "inserted_steps": ",".join(next_step.kind.value for next_step in next_steps),
                    },
                ),
                run_state,
            )
            self._store.save_state(run_state)
            self._store.save_checkpoint(run_state, f"step-{step.id}")

        self._finalize_terminal_run(run_state)
        return run_state

    def _select_next_step(self, run_state: RunState) -> Step | None:
        ready_steps = run_state.ready_steps()
        if ready_steps:
            return ready_steps[0]
        return None

    def _save_artifacts(self, run_state: RunState, step: Step, artifacts: dict[str, str]) -> dict[str, str]:
        saved: dict[str, str] = {}
        for name, content in artifacts.items():
            path = self._store.save_artifact(run_state.run_id, f"{step.id}_{name}", content)
            saved[name] = path
        return saved

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
            run_state.status = RunStatus.FAILED
            run_state.phase = "failed"
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
        if self._lifecycle_bus is None:
            return
        events = self._lifecycle_bus.drain()
        self._update_spawned_agents(run_state, events)

    def _update_spawned_agents(self, run_state: RunState, events: list[AgentLifecycleEvent]) -> None:
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

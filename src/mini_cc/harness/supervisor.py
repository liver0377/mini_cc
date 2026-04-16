from __future__ import annotations

import inspect
import secrets
from collections.abc import Awaitable, Callable

from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.judge import RunJudge
from mini_cc.harness.models import RunState, RunStatus, Step, StepKind, StepResult, StepStatus
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
        event_sink: HarnessEventSink | None = None,
    ) -> None:
        self._store = store
        self._step_runner = step_runner
        self._policy = policy_engine or PolicyEngine()
        self._judge = judge or RunJudge()
        self._event_sink = event_sink

    async def run(self, run_state: RunState) -> RunState:
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
            limit_decision = self._policy.check_run_limits(run_state)
            if limit_decision is not None:
                self._apply_terminal_decision(run_state, limit_decision)
                break

            step = self._select_next_step(run_state)
            if step is None:
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
                    data={"kind": step.kind.value},
                ),
                run_state,
            )
            self._store.save_state(run_state)

            result = await self._step_runner.run_step(step, run_state)
            artifact_paths = self._save_artifacts(run_state, step, result.artifacts)

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
                run_state.status = RunStatus.VERIFYING
            elif step.kind == StepKind.EDIT_CODE:
                run_state.status = RunStatus.RUNNING

            if result.success:
                run_state.failure_count = 0
                run_state.consecutive_no_progress_count = 0 if result.progress_made else (
                    run_state.consecutive_no_progress_count + 1
                )
            else:
                run_state.failure_count += 1
                run_state.consecutive_no_progress_count += 0 if result.progress_made else 1
                if step.kind in {StepKind.RUN_TESTS, StepKind.INSPECT_FAILURES}:
                    run_state.bash_command_count += 1

            health = self._judge.assess(run_state, step, result)
            decision = self._policy.evaluate_step(run_state, step, result, health)
            self._apply_step_decision(run_state, step, result, decision)

            next_steps = list(result.next_steps)
            next_steps.extend(decision.insert_steps)
            if next_steps:
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
                    },
                ),
                run_state,
            )
            self._store.save_state(run_state)
            self._store.save_checkpoint(run_state, f"step-{step.id}")

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
            run_state.phase = "failed"
            event_type = "run_failed"
        run_state.touch()
        self._store.append_event(HarnessEvent(event_type=event_type, run_id=run_state.run_id, message=decision.reason))
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

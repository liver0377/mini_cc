from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from mini_cc.harness.models import RunHealth, RunState, RunStatus, Step, StepKind, StepResult


class PolicyAction(StrEnum):
    CONTINUE = "continue"
    RETRY = "retry"
    REPLAN = "replan"
    BLOCK = "block"
    FAIL = "fail"
    COMPLETE = "complete"
    TIME_OUT = "time_out"


class PolicyDecision(BaseModel):
    action: PolicyAction
    reason: str
    insert_steps: list[Step] = Field(default_factory=list)
    terminal_status: RunStatus | None = None


class PolicyEngine:
    def check_run_limits(self, run_state: RunState) -> PolicyDecision | None:
        now = datetime.now(UTC)
        deadline = run_state.deadline_at
        if deadline is not None and now >= datetime.fromisoformat(deadline):
            return PolicyDecision(
                action=PolicyAction.TIME_OUT,
                reason="run deadline exceeded",
                terminal_status=RunStatus.TIMED_OUT,
            )

        if run_state.test_run_count >= run_state.budget.max_test_runs:
            return PolicyDecision(
                action=PolicyAction.FAIL,
                reason="test run budget exceeded",
                terminal_status=RunStatus.FAILED,
            )

        if run_state.bash_command_count >= run_state.budget.max_bash_commands:
            return PolicyDecision(
                action=PolicyAction.FAIL,
                reason="bash command budget exceeded",
                terminal_status=RunStatus.FAILED,
            )

        return None

    def evaluate_step(self, run_state: RunState, step: Step, result: StepResult, health: RunHealth) -> PolicyDecision:
        if step.kind == StepKind.FINALIZE and result.success:
            return PolicyDecision(
                action=PolicyAction.COMPLETE,
                reason="finalize step succeeded",
                terminal_status=RunStatus.COMPLETED,
            )

        if result.success:
            if health == RunHealth.STALLED and step.kind != StepKind.MAKE_PLAN:
                return PolicyDecision(
                    action=PolicyAction.REPLAN,
                    reason="no progress detected after successful step",
                    insert_steps=[
                        Step(
                            kind=StepKind.MAKE_PLAN,
                            title="Replan",
                            goal="Generate a revised plan based on the latest run state.",
                        )
                    ],
                )
            return PolicyDecision(action=PolicyAction.CONTINUE, reason="step succeeded")

        if step.retry_count < run_state.retry_policy.max_step_retries and result.retryable:
            return PolicyDecision(action=PolicyAction.RETRY, reason="step failed but retry budget remains")

        if health == RunHealth.STALLED and step.kind != StepKind.MAKE_PLAN:
            return PolicyDecision(
                action=PolicyAction.REPLAN,
                reason="step failed without progress; replan requested",
                insert_steps=[
                    Step(
                        kind=StepKind.MAKE_PLAN,
                        title="Replan",
                        goal="Generate a revised plan after repeated failures.",
                    )
                ],
            )

        if health == RunHealth.BLOCKED or run_state.failure_count >= run_state.retry_policy.max_consecutive_failures:
            return PolicyDecision(
                action=PolicyAction.BLOCK,
                reason="run is blocked by repeated failures",
                terminal_status=RunStatus.BLOCKED,
            )

        return PolicyDecision(
            action=PolicyAction.FAIL,
            reason="step failed without remaining retries",
            terminal_status=RunStatus.FAILED,
        )

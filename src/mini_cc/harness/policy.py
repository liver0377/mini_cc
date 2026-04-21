from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from mini_cc.harness.models import FailureClass, RunHealth, RunState, RunStatus, Step, StepKind, StepResult


class PolicyAction(StrEnum):
    CONTINUE = "continue"
    RETRY = "retry"
    COOLDOWN = "cooldown"
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
    cooldown_seconds: int | None = None


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

        if run_state.active_agent_count > run_state.budget.max_active_agents * 2:
            return PolicyDecision(
                action=PolicyAction.BLOCK,
                reason="active agent limit exceeded",
                terminal_status=RunStatus.FAILED,
            )

        if run_state.active_write_agent_count > 1:
            return PolicyDecision(
                action=PolicyAction.BLOCK,
                reason="multiple active write agents are not allowed",
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
                if run_state.replan_count >= run_state.retry_policy.max_replan_count:
                    return PolicyDecision(
                        action=PolicyAction.FAIL,
                        reason="replan limit exceeded",
                        terminal_status=RunStatus.FAILED,
                    )
                return PolicyDecision(
                    action=PolicyAction.REPLAN,
                    reason="no progress detected after successful step",
                    insert_steps=[self._build_replan_step(run_state)],
                )
            return PolicyDecision(action=PolicyAction.CONTINUE, reason="step succeeded")

        if result.failure_class == FailureClass.TRANSIENT_PROVIDER:
            if run_state.provider_cooldown_count >= 3:
                return PolicyDecision(
                    action=PolicyAction.FAIL,
                    reason="provider remained unavailable after repeated cooldowns",
                    terminal_status=RunStatus.FAILED,
                )
            cooldown_seconds = self._provider_cooldown_seconds(run_state.provider_cooldown_count)
            return PolicyDecision(
                action=PolicyAction.COOLDOWN,
                reason=f"provider transient failure; back off for {cooldown_seconds} seconds",
                cooldown_seconds=cooldown_seconds,
            )

        if result.timed_out:
            if step.retry_count < run_state.retry_policy.max_step_retries and result.retryable:
                return PolicyDecision(action=PolicyAction.RETRY, reason="step timed out but retry budget remains")

            if (
                health == RunHealth.BLOCKED
                or run_state.failure_count >= run_state.retry_policy.max_consecutive_failures
            ):
                return PolicyDecision(
                    action=PolicyAction.BLOCK,
                    reason="run is blocked by repeated step timeouts",
                    terminal_status=RunStatus.FAILED,
                )

            if step.kind in {StepKind.RUN_TESTS, StepKind.INSPECT_FAILURES, StepKind.RUN_TASK_AUDIT}:
                if run_state.replan_count >= run_state.retry_policy.max_replan_count:
                    return PolicyDecision(
                        action=PolicyAction.FAIL,
                        reason="replan limit exceeded after step timeout",
                        terminal_status=RunStatus.FAILED,
                    )
                return PolicyDecision(
                    action=PolicyAction.REPLAN,
                    reason="step timed out; revise scope or command before retrying",
                    insert_steps=[self._build_timeout_replan_step(step, result)],
                )

            return PolicyDecision(
                action=PolicyAction.FAIL,
                reason="step timed out without remaining retries",
                terminal_status=RunStatus.FAILED,
            )

        if step.retry_count < run_state.retry_policy.max_step_retries and result.retryable:
            return PolicyDecision(action=PolicyAction.RETRY, reason="step failed but retry budget remains")

        if step.kind in {StepKind.RUN_TESTS, StepKind.INSPECT_FAILURES} and health != RunHealth.BLOCKED:
            if run_state.replan_count >= run_state.retry_policy.max_replan_count:
                return PolicyDecision(
                    action=PolicyAction.FAIL,
                    reason="replan limit exceeded",
                    terminal_status=RunStatus.FAILED,
                )
            return PolicyDecision(
                action=PolicyAction.REPLAN,
                reason="verification failed; gather diagnostics and replan",
            )

        if step.kind == StepKind.RUN_TASK_AUDIT and not result.success:
            if run_state.replan_count >= run_state.retry_policy.max_replan_count:
                return PolicyDecision(
                    action=PolicyAction.FAIL,
                    reason="replan limit exceeded after audit failure",
                    terminal_status=RunStatus.FAILED,
                )
            return PolicyDecision(
                action=PolicyAction.REPLAN,
                reason="task audit failed; replan to address audit findings",
                insert_steps=[self._build_audit_replan_step(result)],
            )

        if health == RunHealth.STALLED and step.kind != StepKind.MAKE_PLAN:
            if run_state.replan_count >= run_state.retry_policy.max_replan_count:
                return PolicyDecision(
                    action=PolicyAction.FAIL,
                    reason="replan limit exceeded",
                    terminal_status=RunStatus.FAILED,
                )
            return PolicyDecision(
                action=PolicyAction.REPLAN,
                reason="step failed without progress; replan requested",
                insert_steps=[self._build_replan_step(run_state)],
            )

        if health == RunHealth.BLOCKED or run_state.failure_count >= run_state.retry_policy.max_consecutive_failures:
            return PolicyDecision(
                action=PolicyAction.BLOCK,
                reason="run is blocked by repeated failures",
                terminal_status=RunStatus.FAILED,
            )

        return PolicyDecision(
            action=PolicyAction.FAIL,
            reason="step failed without remaining retries",
            terminal_status=RunStatus.FAILED,
        )

    def _build_replan_step(self, run_state: RunState) -> Step:
        goal = "Generate a revised plan based on the latest run state."
        next_focus = run_state.metadata.get("audit_next_focus")
        agent_issue = run_state.metadata.get("agent_latest_issue")
        if next_focus:
            goal = f"Generate a revised plan. Audit recommends focusing on: {next_focus}"
        if agent_issue:
            goal += f" Also account for this recent sub-agent issue: {agent_issue}."
        return Step(
            kind=StepKind.MAKE_PLAN,
            title="Replan",
            goal=goal,
        )

    def _build_audit_replan_step(self, result: StepResult) -> Step:
        goal = "Generate a revised plan to address task audit findings."
        blockers = result.metadata.get("audit_blockers", "")
        next_focus = result.metadata.get("audit_next_focus")
        constraints: list[str] = []
        if blockers.strip():
            constraints.append(f"Resolve audit blockers: {blockers}")
        if next_focus:
            constraints.append(f"Focus on: {next_focus}")
        if constraints:
            goal += f" Constraints: {'; '.join(constraints)}."
        return Step(
            kind=StepKind.MAKE_PLAN,
            title="Audit Replan",
            goal=goal,
        )

    def _build_timeout_replan_step(self, step: Step, result: StepResult) -> Step:
        timeout_seconds = result.metadata.get("timeout_seconds", "unknown")
        return Step(
            kind=StepKind.MAKE_PLAN,
            title="Timeout Replan",
            goal=(
                "Generate a revised plan after a step timed out. "
                f"The timed out step was `{step.kind.value}` with a budget of {timeout_seconds} seconds. "
                "Reduce scope, split work, or choose a faster verification command before retrying."
            ),
        )

    def _provider_cooldown_seconds(self, attempt: int) -> int:
        schedule = [30, 120, 300]
        return schedule[min(attempt, len(schedule) - 1)]

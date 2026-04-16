from __future__ import annotations

from mini_cc.harness.models import RunHealth, RunState, Step, StepResult


class RunJudge:
    def assess(self, run_state: RunState, step: Step, result: StepResult) -> RunHealth:
        if result.progress_made:
            return RunHealth.PROGRESSING
        if not result.success and run_state.failure_count >= run_state.retry_policy.max_consecutive_failures:
            return RunHealth.BLOCKED
        if run_state.consecutive_no_progress_count >= run_state.retry_policy.max_consecutive_no_progress:
            return RunHealth.STALLED
        if not result.success:
            return RunHealth.REGRESSING
        return RunHealth.STALLED


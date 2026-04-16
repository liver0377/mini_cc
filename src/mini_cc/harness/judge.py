from __future__ import annotations

import json
from pathlib import Path

from mini_cc.harness.models import RunHealth, RunState, Step, StepKind, StepResult


class RunJudge:
    def assess(self, run_state: RunState, step: Step, result: StepResult) -> RunHealth:
        if step.kind == StepKind.RUN_TASK_AUDIT:
            return self._assess_audit(run_state, result)
        if result.timed_out:
            if run_state.failure_count >= run_state.retry_policy.max_consecutive_failures:
                return RunHealth.BLOCKED
            return RunHealth.STALLED
        if result.progress_made:
            return RunHealth.PROGRESSING
        if not result.success and run_state.failure_count >= run_state.retry_policy.max_consecutive_failures:
            return RunHealth.BLOCKED
        if run_state.consecutive_no_progress_count >= run_state.retry_policy.max_consecutive_no_progress:
            return RunHealth.STALLED
        if not result.success:
            return RunHealth.REGRESSING
        return RunHealth.STALLED

    def _assess_audit(self, run_state: RunState, result: StepResult) -> RunHealth:
        if result.timed_out:
            if run_state.failure_count >= run_state.retry_policy.max_consecutive_failures:
                return RunHealth.BLOCKED
            return RunHealth.STALLED
        if not result.success:
            return RunHealth.REGRESSING
        regressions = result.metadata.get("audit_regressions", "")
        blockers = result.metadata.get("audit_blockers", "")
        if regressions.strip():
            return RunHealth.REGRESSING
        if blockers.strip() and self._blocker_repeated(run_state, blockers):
            return RunHealth.BLOCKED
        if result.progress_made:
            return RunHealth.PROGRESSING
        return RunHealth.STALLED

    def _blocker_repeated(self, run_state: RunState, current_blockers: str) -> bool:
        if run_state.consecutive_no_progress_count < 2:
            return False
        current_first = current_blockers.split("|")[0].strip()
        if not current_first:
            return False
        for step in reversed(run_state.steps):
            if step.kind == StepKind.RUN_TASK_AUDIT and step.status.value == "succeeded":
                prev_blockers = self._read_blockers_from_step(step)
                if prev_blockers and current_first in prev_blockers.split(" | "):
                    return True
        return False

    def _read_blockers_from_step(self, step: Step) -> str | None:
        for artifact_path in step.artifacts.values():
            path = Path(artifact_path)
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                blockers = data.get("blockers")
                if isinstance(blockers, list) and blockers:
                    return " | ".join(str(b) for b in blockers)
        return None

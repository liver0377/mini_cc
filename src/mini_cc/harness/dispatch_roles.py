from __future__ import annotations

from mini_cc.harness.models import StepKind


def role_for_step(kind: StepKind) -> str:
    if kind in {StepKind.BOOTSTRAP_PROJECT, StepKind.EDIT_CODE}:
        return "implementer"
    if kind in {StepKind.ANALYZE_REPO, StepKind.MAKE_PLAN}:
        return "analyzer" if kind == StepKind.ANALYZE_REPO else "planner"
    if kind in {StepKind.RUN_TESTS, StepKind.RUN_TASK_AUDIT, StepKind.INSPECT_FAILURES}:
        return "verifier"
    if kind in {StepKind.SUMMARIZE_PROGRESS, StepKind.FINALIZE}:
        return "reporter"
    if kind == StepKind.SPAWN_READONLY_AGENT:
        return "analyzer"
    return "implementer"

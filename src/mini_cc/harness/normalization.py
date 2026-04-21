from __future__ import annotations

from mini_cc.harness.dispatch_roles import role_for_step
from mini_cc.harness.models import Step, WorkItem

DEFAULT_WORK_ITEM_METADATA = "default_step_work_item"


def normalize_step_work_items(step: Step) -> Step:
    if step.work_items:
        return step
    step.work_items = [
        WorkItem(
            id=f"{step.id}.main",
            kind=step.kind.value,
            title=step.title,
            goal=step.goal,
            role=role_for_step(step.kind),
            inputs=dict(step.inputs),
            budget_seconds=step.budget_seconds,
            metadata={DEFAULT_WORK_ITEM_METADATA: "true"},
        )
    ]
    return step


def normalize_steps_work_items(steps: list[Step]) -> list[Step]:
    return [normalize_step_work_items(step) for step in steps]

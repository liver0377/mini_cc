from __future__ import annotations

from dataclasses import dataclass

from mini_cc.harness.models import RunState, Step, WorkItem
from mini_cc.harness.normalization import normalize_steps_work_items

_ROLE_PRIORITY: dict[str, int] = {
    "verifier": 50,
    "implementer": 40,
    "analyzer": 30,
    "planner": 20,
    "reporter": 10,
}


@dataclass(frozen=True)
class ExecutionCandidate:
    step: Step
    work_item: WorkItem
    role: str
    priority: int
    item_index: int


@dataclass(frozen=True)
class SchedulingDecision:
    selected: ExecutionCandidate
    considered_count: int
    reason: str


class Scheduler:
    def decide(self, run_state: RunState) -> SchedulingDecision | None:
        run_state.steps = normalize_steps_work_items(run_state.steps)
        ready_steps = run_state.ready_steps()
        if not ready_steps:
            return None

        step: Step | None = None
        ready_items: list[WorkItem] = []
        for candidate_step in ready_steps:
            items = candidate_step.ready_work_items()
            if items:
                step = candidate_step
                ready_items = items
                break
        if step is None:
            return None

        candidates: list[ExecutionCandidate] = []
        for item_index, work_item in enumerate(ready_items):
            role = work_item.role
            candidates.append(
                ExecutionCandidate(
                    step=step,
                    work_item=work_item,
                    role=role,
                    priority=self._candidate_priority(role, work_item, item_index),
                    item_index=item_index,
                )
            )

        candidates.sort(key=lambda c: (c.priority, -c.item_index), reverse=True)
        selected = candidates[0]
        return SchedulingDecision(
            selected=selected,
            considered_count=len(candidates),
            reason=f"selected {selected.work_item.id} as highest-priority {selected.role} candidate in step {step.id}",
        )

    def select_next_execution(self, run_state: RunState) -> tuple[Step, WorkItem] | None:
        decision = self.decide(run_state)
        if decision is None:
            return None
        return decision.selected.step, decision.selected.work_item

    def _candidate_priority(self, role: str, work_item: WorkItem, item_index: int) -> int:
        base = _ROLE_PRIORITY.get(role, 0)
        aging_bonus = max(0, 5 - item_index)
        retry_penalty = work_item.retry_count * 5
        return base + aging_bonus - retry_penalty

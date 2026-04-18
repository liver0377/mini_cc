from __future__ import annotations

from dataclasses import dataclass

from mini_cc.harness.dispatch_roles import role_for_step
from mini_cc.harness.models import RunState, Step, StepKind, StepStatus, WorkItem

_READONLY_ROLES = frozenset({"analyzer", "planner", "reporter", "verifier"})

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
    work_item: WorkItem | None
    role: str
    priority: int
    step_index: int
    item_index: int


@dataclass(frozen=True)
class RejectedCandidate:
    step: Step
    work_item: WorkItem | None
    role: str
    reason: str


@dataclass(frozen=True)
class SchedulingDecision:
    selected: ExecutionCandidate
    considered_count: int
    reason: str
    rejected: list[RejectedCandidate]


@dataclass(frozen=True)
class BatchSchedulingDecision:
    candidates: list[ExecutionCandidate]
    considered_count: int
    reason: str
    rejected: list[RejectedCandidate]


class Scheduler:
    def decide(self, run_state: RunState) -> SchedulingDecision | None:
        ready_steps = run_state.ready_steps()
        if not ready_steps:
            return None
        candidates: list[ExecutionCandidate] = []
        rejected: list[RejectedCandidate] = []
        for step_index, step in enumerate(ready_steps):
            if step.has_work_items:
                ready_items = step.ready_work_items()
                for item_index, ready_item in enumerate(ready_items):
                    role = ready_item.role
                    allowed, rejected_reason = self._candidate_allowed(run_state, step, ready_item, role)
                    if not allowed:
                        rejected.append(
                            RejectedCandidate(
                                step=step,
                                work_item=ready_item,
                                role=role,
                                reason=rejected_reason,
                            )
                        )
                        continue
                    candidates.append(
                        ExecutionCandidate(
                            step=step,
                            work_item=ready_item,
                            role=role,
                            priority=self._candidate_priority(
                                run_state,
                                step,
                                ready_item,
                                role,
                                step_index,
                                item_index,
                            ),
                            step_index=step_index,
                            item_index=item_index,
                        )
                    )
                if step.has_terminal_work_item_failure():
                    role = role_for_step(step.kind)
                    candidates.append(
                        ExecutionCandidate(
                            step=step,
                            work_item=None,
                            role=role,
                            priority=self._candidate_priority(run_state, step, None, role, step_index, 0),
                            step_index=step_index,
                            item_index=0,
                        )
                    )
            else:
                role = role_for_step(step.kind)
                allowed, rejected_reason = self._candidate_allowed(run_state, step, None, role)
                if not allowed:
                    rejected.append(
                        RejectedCandidate(
                            step=step,
                            work_item=None,
                            role=role,
                            reason=rejected_reason,
                        )
                    )
                    continue
                candidates.append(
                    ExecutionCandidate(
                        step=step,
                        work_item=None,
                        role=role,
                        priority=self._candidate_priority(run_state, step, None, role, step_index, 0),
                        step_index=step_index,
                        item_index=0,
                    )
                )
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (item.priority, -item.step_index, -item.item_index),
            reverse=True,
        )
        selected = candidates[0]
        return SchedulingDecision(
            selected=selected,
            considered_count=len(candidates),
            reason=self._decision_reason(selected, rejected),
            rejected=rejected,
        )

    def decide_readonly_batch(self, run_state: RunState) -> BatchSchedulingDecision | None:
        remaining_capacity = run_state.budget.max_active_agents - run_state.active_readonly_agent_count
        if remaining_capacity <= 0:
            return None

        ready_steps = run_state.ready_steps()
        if not ready_steps:
            return None
        readonly_candidates: list[ExecutionCandidate] = []
        non_readonly_candidates: list[ExecutionCandidate] = []
        rejected: list[RejectedCandidate] = []
        for step_index, step in enumerate(ready_steps):
            if step.has_work_items:
                ready_items = step.ready_work_items()
                for item_index, ready_item in enumerate(ready_items):
                    role = ready_item.role
                    if role in _READONLY_ROLES:
                        allowed, rejected_reason = self._candidate_allowed(run_state, step, ready_item, role)
                        if not allowed:
                            rejected.append(
                                RejectedCandidate(
                                    step=step,
                                    work_item=ready_item,
                                    role=role,
                                    reason=rejected_reason,
                                )
                            )
                            continue
                        readonly_candidates.append(
                            ExecutionCandidate(
                                step=step,
                                work_item=ready_item,
                                role=role,
                                priority=self._candidate_priority(
                                    run_state, step, ready_item, role, step_index, item_index
                                ),
                                step_index=step_index,
                                item_index=item_index,
                            )
                        )
                    else:
                        non_readonly_candidates.append(
                            ExecutionCandidate(
                                step=step,
                                work_item=ready_item,
                                role=role,
                                priority=self._candidate_priority(
                                    run_state, step, ready_item, role, step_index, item_index
                                ),
                                step_index=step_index,
                                item_index=item_index,
                            )
                        )

        if not readonly_candidates:
            return None

        readonly_candidates.sort(
            key=lambda item: (item.priority, -item.step_index, -item.item_index),
            reverse=True,
        )
        selected = readonly_candidates[:remaining_capacity]
        reason = f"batch selected {len(selected)} readonly candidates (capacity={remaining_capacity})"
        return BatchSchedulingDecision(
            candidates=selected,
            considered_count=len(readonly_candidates) + len(non_readonly_candidates),
            reason=reason,
            rejected=rejected,
        )

    def select_next_execution(self, run_state: RunState) -> tuple[Step, WorkItem | None] | None:
        decision = self.decide(run_state)
        if decision is None:
            return None
        return decision.selected.step, decision.selected.work_item

    def _candidate_priority(
        self,
        run_state: RunState,
        step: Step,
        work_item: WorkItem | None,
        role: str,
        step_index: int,
        item_index: int,
    ) -> int:
        if step.kind == StepKind.MAKE_PLAN:
            return 1000 + max(0, 10 - step_index)
        base = _ROLE_PRIORITY.get(role, 0)
        if role == "verifier" and self._has_prior_pending_implementer(run_state, step.id):
            base -= 100
        retry_penalty = (work_item.retry_count if work_item is not None else step.retry_count) * 5
        aging_bonus = max(0, 10 - step_index) + max(0, 5 - item_index)
        return base + aging_bonus - retry_penalty

    def _candidate_allowed(
        self,
        run_state: RunState,
        step: Step,
        work_item: WorkItem | None,
        role: str,
    ) -> tuple[bool, str]:
        del work_item
        if role == "implementer" and run_state.active_write_agent_count >= 1:
            return False, "write capacity is full"
        if role != "implementer" and step.kind == StepKind.SPAWN_READONLY_AGENT:
            if run_state.active_readonly_agent_count >= run_state.budget.max_active_agents:
                return False, "readonly capacity is full"
        if role in _READONLY_ROLES and role != "implementer":
            if run_state.active_readonly_agent_count >= run_state.budget.max_active_agents:
                return False, "readonly capacity is full"
        return True, ""

    def _has_prior_pending_implementer(self, run_state: RunState, step_id: str) -> bool:
        for step in run_state.steps:
            if step.id == step_id:
                return False
            if step.status not in {StepStatus.PENDING, StepStatus.IN_PROGRESS}:
                continue
            if role_for_step(step.kind) == "implementer":
                return True
        return False

    def _decision_reason(self, selected: ExecutionCandidate, rejected: list[RejectedCandidate]) -> str:
        target = selected.work_item.id if selected.work_item is not None else selected.step.id
        reason = f"selected {target} as highest-priority {selected.role} candidate"
        if rejected:
            top_rejected = rejected[0]
            rejected_target = top_rejected.work_item.id if top_rejected.work_item is not None else top_rejected.step.id
            reason += f"; rejected {rejected_target} because {top_rejected.reason}"
        return reason

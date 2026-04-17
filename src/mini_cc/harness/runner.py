from __future__ import annotations

import secrets
import threading

from mini_cc.context.engine_context import EngineContext
from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.judge import RunJudge
from mini_cc.harness.models import (
    RetryPolicy,
    RunBudget,
    RunState,
    RunStatus,
    Step,
    StepKind,
    StepStatus,
    deadline_after,
    utc_now_iso,
)
from mini_cc.harness.policy import PolicyEngine
from mini_cc.harness.step_runner import QueryEventSink, StepRunner
from mini_cc.harness.supervisor import HarnessEventSink, SupervisorLoop
from mini_cc.runtime.agents import AgentEventBus
from mini_cc.tools.bash import Bash


class RunHarness:
    def __init__(
        self,
        *,
        store: CheckpointStore | None = None,
        step_runner: StepRunner,
        policy_engine: PolicyEngine | None = None,
        judge: RunJudge | None = None,
        event_sink: HarnessEventSink | None = None,
        lifecycle_bus: AgentEventBus | None = None,
    ) -> None:
        self._store = store or CheckpointStore()
        self._lifecycle_bus = lifecycle_bus
        self._step_runner = step_runner
        self._run_interrupts: dict[str, threading.Event] = {}
        self._supervisor = SupervisorLoop(
            store=self._store,
            step_runner=step_runner,
            policy_engine=policy_engine,
            judge=judge,
            event_sink=event_sink,
            lifecycle_bus=lifecycle_bus,
        )

    @classmethod
    def create_default(
        cls,
        *,
        engine_ctx: EngineContext | None = None,
        store: CheckpointStore | None = None,
        event_sink: HarnessEventSink | None = None,
        query_event_sink: QueryEventSink | None = None,
    ) -> RunHarness:
        return cls(
            store=store,
            step_runner=StepRunner(
                engine_ctx=engine_ctx,
                bash_tool=Bash(),
                query_event_sink=query_event_sink,
            ),
            event_sink=event_sink,
            lifecycle_bus=engine_ctx.lifecycle_bus if engine_ctx is not None else None,
        )

    async def run(
        self,
        goal: str,
        *,
        steps: list[Step] | None = None,
        budget: RunBudget | None = None,
        retry_policy: RetryPolicy | None = None,
        metadata: dict[str, str] | None = None,
    ) -> RunState:
        run_state = self.create_run(
            goal,
            steps=steps,
            budget=budget,
            retry_policy=retry_policy,
            metadata=metadata,
        )
        interrupt_event = self._run_interrupts.setdefault(run_state.run_id, threading.Event())
        try:
            return await self._supervisor.run_with_interrupt(run_state, interrupt_event=interrupt_event)
        finally:
            self._run_interrupts.pop(run_state.run_id, None)

    async def resume(self, run_id: str) -> RunState:
        run_state = self._store.load_state(run_id)
        if run_state.is_terminal:
            return run_state
        invalidated_agent_ids = self._invalidate_inflight_agents(run_state)
        recovered_step_ids = self._recover_interrupted_steps(run_state)
        if invalidated_agent_ids or recovered_step_ids:
            self._insert_resume_replan_step(
                run_state,
                invalidated_agent_ids=invalidated_agent_ids,
                recovered_step_ids=recovered_step_ids,
            )
            self._store.append_event(
                HarnessEvent(
                    event_type="run_resumed",
                    run_id=run_id,
                    message=(
                        f"invalidated {len(invalidated_agent_ids)} inflight agents and "
                        f"recovered {len(recovered_step_ids)} interrupted steps; inserted replanning step"
                    ),
                    data={
                        "invalidated_agents": str(len(invalidated_agent_ids)),
                        "recovered_steps": str(len(recovered_step_ids)),
                        "invalidated_agent_ids": ",".join(invalidated_agent_ids),
                        "recovered_step_ids": ",".join(recovered_step_ids),
                        "decision": "resume_replan",
                        "decision_reason": "resume recovered interrupted state and inserted replanning step",
                    },
                )
            )
        run_state.touch()
        self._store.save_state(run_state)
        interrupt_event = self._run_interrupts.setdefault(run_state.run_id, threading.Event())
        try:
            return await self._supervisor.run_with_interrupt(run_state, interrupt_event=interrupt_event)
        finally:
            self._run_interrupts.pop(run_state.run_id, None)

    def create_run(
        self,
        goal: str,
        *,
        steps: list[Step] | None = None,
        budget: RunBudget | None = None,
        retry_policy: RetryPolicy | None = None,
        metadata: dict[str, str] | None = None,
    ) -> RunState:
        effective_budget = budget or RunBudget()
        effective_steps = self._normalize_steps(steps or self._default_steps(goal))
        run_id = secrets.token_hex(6)
        run_state = RunState(
            run_id=run_id,
            goal=goal,
            budget=effective_budget,
            retry_policy=retry_policy or RetryPolicy(),
            steps=effective_steps,
            deadline_at=deadline_after(effective_budget.max_runtime_seconds),
            metadata=metadata or {},
        )
        self._store.save_state(run_state)
        self._store.append_event(HarnessEvent(event_type="run_created", run_id=run_id, message=goal))
        return run_state

    def cancel(self, run_id: str) -> RunState:
        run_state = self._store.load_state(run_id)
        interrupt_event = self._run_interrupts.setdefault(run_id, threading.Event())
        interrupt_event.set()
        cancelled_agents = self._step_runner.cancel_active_agents(
            [agent.agent_id for agent in run_state.spawned_agents if agent.completed_at is None]
        )
        run_state.status = RunStatus.CANCELLED
        run_state.phase = "cancelled"
        run_state.touch()
        self._store.save_state(run_state)
        self._store.append_event(
            HarnessEvent(
                event_type="run_cancelled",
                run_id=run_id,
                message=run_state.goal,
                data={"cancelled_agents": ",".join(cancelled_agents)},
            )
        )
        return run_state

    def latest_run_id(self) -> str | None:
        return self._store.latest_run_id()

    @property
    def store(self) -> CheckpointStore:
        return self._store

    def _default_steps(self, goal: str) -> list[Step]:
        return [
            Step(
                kind=StepKind.MAKE_PLAN,
                title="Initial Plan",
                goal=f"Create a concise execution plan for: {goal}",
            )
        ]

    def _normalize_steps(self, steps: list[Step]) -> list[Step]:
        normalized: list[Step] = []
        for index, step in enumerate(steps, start=1):
            copied = step.model_copy(deep=True)
            if not copied.id:
                copied.id = f"step-{index:04d}"
            normalized.append(copied)
        return normalized

    def _invalidate_inflight_agents(self, run_state: RunState) -> list[str]:
        invalidated_ids: list[str] = []
        invalidated_at = utc_now_iso()
        for agent in run_state.spawned_agents:
            if agent.completed_at is not None:
                continue
            agent.completed_at = invalidated_at
            agent.success = False
            agent.termination_reason = "invalidated_on_resume"
            agent.invalidated_on_resume = True
            invalidated_ids.append(agent.agent_id)
        return invalidated_ids

    def _recover_interrupted_steps(self, run_state: RunState) -> list[str]:
        recovered_step_ids: list[str] = []
        for step in run_state.steps:
            if step.status == StepStatus.IN_PROGRESS:
                step.status = StepStatus.PENDING
                step.error = "step interrupted before completion; recovered during resume"
                run_state.sync_step(step)
                recovered_step_ids.append(step.id)
        run_state.current_step_id = None
        if not run_state.is_terminal:
            run_state.status = RunStatus.RUNNING
            run_state.phase = "resumed"
        return recovered_step_ids

    def _insert_resume_replan_step(
        self,
        run_state: RunState,
        *,
        invalidated_agent_ids: list[str],
        recovered_step_ids: list[str],
    ) -> None:
        has_pending_resume_replan = any(
            step.id.startswith("step-resume-") and step.status == StepStatus.PENDING for step in run_state.steps
        )
        if has_pending_resume_replan:
            return
        recovered_steps_text = ", ".join(recovered_step_ids) or "none"
        invalidated_agents_text = ", ".join(invalidated_agent_ids) or "none"
        resume_step = Step(
            id=f"step-resume-{secrets.token_hex(4)}",
            kind=StepKind.MAKE_PLAN,
            title="Resume Replan",
            goal=(
                "Resume the run after interruption. Reassess the current state, account for invalidated child "
                "agents, recovered interrupted steps, and decide whether to re-dispatch readonly agents or "
                "continue with the existing plan."
            ),
            inputs={
                "prompt": (
                    "The run was resumed after interruption. "
                    f"Invalidated child agents: {invalidated_agents_text}. "
                    f"Recovered interrupted steps: {recovered_steps_text}. "
                    "Review the latest state and produce the next safe plan."
                )
            },
        )
        insert_at = len(run_state.steps)
        for i, s in enumerate(run_state.steps):
            if s.status == StepStatus.PENDING:
                insert_at = i
                break
        run_state.steps.insert(insert_at, resume_step)

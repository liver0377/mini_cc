from __future__ import annotations

import secrets

from mini_cc.context.engine_context import EngineContext
from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.judge import RunJudge
from mini_cc.harness.models import RetryPolicy, RunBudget, RunState, RunStatus, Step, StepKind, deadline_after
from mini_cc.harness.policy import PolicyEngine
from mini_cc.harness.step_runner import QueryEventSink, StepRunner
from mini_cc.harness.supervisor import HarnessEventSink, SupervisorLoop


class RunHarness:
    def __init__(
        self,
        *,
        store: CheckpointStore | None = None,
        step_runner: StepRunner,
        policy_engine: PolicyEngine | None = None,
        judge: RunJudge | None = None,
        event_sink: HarnessEventSink | None = None,
    ) -> None:
        self._store = store or CheckpointStore()
        self._supervisor = SupervisorLoop(
            store=self._store,
            step_runner=step_runner,
            policy_engine=policy_engine,
            judge=judge,
            event_sink=event_sink,
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
            step_runner=StepRunner(engine_ctx=engine_ctx, query_event_sink=query_event_sink),
            event_sink=event_sink,
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
        return await self._supervisor.run(run_state)

    async def resume(self, run_id: str) -> RunState:
        run_state = self._store.load_state(run_id)
        return await self._supervisor.run(run_state)

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
        run_state.status = RunStatus.CANCELLED
        run_state.phase = "cancelled"
        run_state.touch()
        self._store.save_state(run_state)
        self._store.append_event(HarnessEvent(event_type="run_cancelled", run_id=run_id, message=run_state.goal))
        return run_state

    def latest_run_id(self) -> str | None:
        return self._store.latest_run_id()

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

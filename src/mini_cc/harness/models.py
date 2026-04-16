from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from mini_cc.models import QueryState

ScalarValue = str | int | bool
StepInputs = dict[str, ScalarValue]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def deadline_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def parse_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class StepKind(StrEnum):
    ANALYZE_REPO = "analyze_repo"
    MAKE_PLAN = "make_plan"
    EDIT_CODE = "edit_code"
    RUN_TESTS = "run_tests"
    INSPECT_FAILURES = "inspect_failures"
    SPAWN_READONLY_AGENT = "spawn_readonly_agent"
    SUMMARIZE_PROGRESS = "summarize_progress"
    CHECKPOINT = "checkpoint"
    FINALIZE = "finalize"


class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    SKIPPED = "skipped"


class RunHealth(StrEnum):
    PROGRESSING = "progressing"
    STALLED = "stalled"
    BLOCKED = "blocked"
    REGRESSING = "regressing"


class RunBudget(BaseModel):
    max_runtime_seconds: int = 3600
    max_step_seconds: int = 300
    max_test_runs: int = 20
    max_bash_commands: int = 50
    max_active_agents: int = 2


class RetryPolicy(BaseModel):
    max_step_retries: int = 2
    max_consecutive_failures: int = 3
    max_consecutive_no_progress: int = 3


class Step(BaseModel):
    id: str = ""
    kind: StepKind
    title: str
    goal: str
    inputs: StepInputs = Field(default_factory=dict)
    expected_output: str = ""
    status: StepStatus = StepStatus.PENDING
    retry_count: int = 0
    budget_seconds: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    evaluation: str = ""
    summary: str = ""
    error: str | None = None


class StepResult(BaseModel):
    success: bool
    summary: str
    artifacts: dict[str, str] = Field(default_factory=dict)
    next_steps: list[Step] = Field(default_factory=list)
    retryable: bool = True
    error: str | None = None
    progress_made: bool = False
    query_state: QueryState | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class RunSummary(BaseModel):
    run_id: str
    status: RunStatus
    goal: str
    latest_summary: str = ""


class RunState(BaseModel):
    run_id: str
    goal: str
    status: RunStatus = RunStatus.CREATED
    phase: str = "created"
    created_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    deadline_at: str | None = None
    updated_at: str = Field(default_factory=utc_now_iso)
    budget: RunBudget = Field(default_factory=RunBudget)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    steps: list[Step] = Field(default_factory=list)
    current_step_id: str | None = None
    completed_step_ids: list[str] = Field(default_factory=list)
    failed_step_ids: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    latest_summary: str = ""
    latest_query_state: QueryState | None = None
    failure_count: int = 0
    consecutive_no_progress_count: int = 0
    test_run_count: int = 0
    bash_command_count: int = 0
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def get_step(self, step_id: str) -> Step | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def ready_steps(self) -> list[Step]:
        completed = set(self.completed_step_ids)
        return [
            step
            for step in self.steps
            if step.status == StepStatus.PENDING and all(dep in completed for dep in step.depends_on)
        ]

    def pending_steps(self) -> list[Step]:
        return [step for step in self.steps if step.status == StepStatus.PENDING]

    def sync_step(self, updated_step: Step) -> None:
        for index, step in enumerate(self.steps):
            if step.id == updated_step.id:
                self.steps[index] = updated_step
                return
        self.steps.append(updated_step)

    def append_steps(self, steps: list[Step]) -> None:
        self.steps.extend(steps)

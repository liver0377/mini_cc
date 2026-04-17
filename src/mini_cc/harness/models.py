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


def format_local_time(value: str) -> str:
    dt = datetime.fromisoformat(value).astimezone()
    offset = dt.utcoffset() or timedelta()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    tz_name = dt.tzname() or "local"
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {tz_name} (UTC{sign}{hours:02d}:{minutes:02d})"


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    COOLDOWN = "cooldown"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class StepKind(StrEnum):
    BOOTSTRAP_PROJECT = "bootstrap_project"
    ANALYZE_REPO = "analyze_repo"
    MAKE_PLAN = "make_plan"
    EDIT_CODE = "edit_code"
    RUN_TESTS = "run_tests"
    RUN_TASK_AUDIT = "run_task_audit"
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


class WorkItemStatus(StrEnum):
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


class FailureClass(StrEnum):
    TRANSIENT_PROVIDER = "transient_provider"
    TRANSIENT_ENV = "transient_env"
    TOOL_FAILURE = "tool_failure"
    LOGIC_FAILURE = "logic_failure"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    CANCELLED = "cancelled"
    HUMAN_BLOCKED = "human_blocked"


class TraceSpan(BaseModel):
    span_id: str
    run_id: str
    step_id: str | None = None
    work_item_id: str | None = None
    parent_span_id: str | None = None
    kind: str
    name: str
    status: str
    start_at: str | None = None
    end_at: str | None = None
    duration_ms: int | None = None
    summary: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class SchedulerDecisionRecord(BaseModel):
    run_id: str
    step_id: str
    work_item_id: str | None = None
    selected_role: str
    selected_priority: int
    considered_count: int
    reason: str
    rejected_targets: list[str] = Field(default_factory=list)
    rejected_reasons: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


class AgentTrace(BaseModel):
    agent_id: str
    source_step_id: str | None = None
    readonly: bool = False
    scope_paths: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    completed_at: str | None = None
    success: bool | None = None
    termination_reason: str | None = None
    output_preview: str = ""
    output_path: str | None = None
    is_stale: bool = False
    base_version_stamp: str = ""
    completed_version_stamp: str = ""
    invalidated_on_resume: bool = False


class AgentBudget(BaseModel):
    max_readonly: int = 5
    max_write: int = 1
    remaining_readonly: int = 5
    remaining_write: int = 1


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
    max_replan_count: int = 3


class WorkItem(BaseModel):
    id: str = ""
    kind: str
    title: str
    goal: str
    role: str
    inputs: StepInputs = Field(default_factory=dict)
    status: WorkItemStatus = WorkItemStatus.PENDING
    retry_count: int = 0
    budget_seconds: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    error: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


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
    work_items: list[WorkItem] = Field(default_factory=list)

    @property
    def has_work_items(self) -> bool:
        return bool(self.work_items)

    def ready_work_items(self) -> list[WorkItem]:
        completed = {item.id for item in self.work_items if item.status == WorkItemStatus.SUCCEEDED}
        return [
            item
            for item in self.work_items
            if item.status == WorkItemStatus.PENDING and all(dep in completed for dep in item.depends_on)
        ]

    def pending_work_items(self) -> list[WorkItem]:
        return [item for item in self.work_items if item.status == WorkItemStatus.PENDING]

    def sync_work_item(self, updated_item: WorkItem) -> None:
        for index, item in enumerate(self.work_items):
            if item.id == updated_item.id:
                self.work_items[index] = updated_item
                return
        self.work_items.append(updated_item)

    def all_work_items_succeeded(self) -> bool:
        return bool(self.work_items) and all(item.status == WorkItemStatus.SUCCEEDED for item in self.work_items)

    def has_terminal_work_item_failure(self) -> bool:
        return any(item.status == WorkItemStatus.FAILED_TERMINAL for item in self.work_items)


class StepResult(BaseModel):
    success: bool
    summary: str
    artifacts: dict[str, str] = Field(default_factory=dict)
    next_steps: list[Step] = Field(default_factory=list)
    retryable: bool = True
    error: str | None = None
    timed_out: bool = False
    progress_made: bool = False
    query_state: QueryState | None = None
    failure_class: FailureClass | None = None
    trace_spans: list[TraceSpan] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class RunState(BaseModel):
    run_id: str
    goal: str
    status: RunStatus = RunStatus.CREATED
    phase: str = "created"
    created_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    deadline_at: str | None = None
    cooldown_until: str | None = None
    cooldown_reason: str | None = None
    updated_at: str = Field(default_factory=utc_now_iso)
    budget: RunBudget = Field(default_factory=RunBudget)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    steps: list[Step] = Field(default_factory=list)
    current_step_id: str | None = None
    current_work_item_id: str | None = None
    completed_step_ids: list[str] = Field(default_factory=list)
    failed_step_ids: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    latest_summary: str = ""
    latest_query_state: QueryState | None = None
    failure_count: int = 0
    consecutive_no_progress_count: int = 0
    test_run_count: int = 0
    bash_command_count: int = 0
    spawned_agents: list[AgentTrace] = Field(default_factory=list)
    agent_budget: AgentBudget | None = None
    replan_count: int = 0
    provider_cooldown_count: int = 0
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunStatus.BLOCKED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }

    @property
    def active_agent_count(self) -> int:
        return sum(1 for a in self.spawned_agents if a.completed_at is None)

    @property
    def active_readonly_agent_count(self) -> int:
        return sum(1 for a in self.spawned_agents if a.completed_at is None and a.readonly)

    @property
    def active_write_agent_count(self) -> int:
        return sum(1 for a in self.spawned_agents if a.completed_at is None and not a.readonly)

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
            if step.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}
            and all(dep in completed for dep in step.depends_on)
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

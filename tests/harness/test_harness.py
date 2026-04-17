from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

from mini_cc.agent.bus import AgentEventBus
from mini_cc.context.engine_context import EngineContext
from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.harness import (
    AgentBudget,
    AgentTrace,
    CheckpointStore,
    IterationOptimizer,
    IterationOutcome,
    IterationReview,
    IterationScore,
    IterationSnapshot,
    RunBudget,
    RunDocGenerator,
    RunHarness,
    RunHealth,
    RunJudge,
    RunState,
    RunStatus,
    Step,
    StepKind,
    StepResult,
    StepRunner,
    StepStatus,
)
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.models import format_local_time
from mini_cc.harness.policy import PolicyAction, PolicyDecision, PolicyEngine
from mini_cc.harness.step_runner import _QueryDiagnostics
from mini_cc.harness.supervisor import SupervisorLoop
from mini_cc.harness.task_audit import TaskAuditRegistry
from mini_cc.models import (
    AgentCompletionEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    Message,
    Role,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.query_engine.engine import QueryEngine


async def _noop_execute(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
    return
    yield


async def _stream_text(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
    prompt = ""
    for message in reversed(messages):
        if message.role == Role.USER and message.content:
            prompt = message.content
            break
    yield TextDelta(content=f"handled:{prompt}")


def _make_engine_ctx(tmp_path) -> EngineContext:
    engine = QueryEngine(
        stream_fn=_stream_text,
        tool_use_ctx=ToolUseContext(
            get_schemas=lambda: [],
            execute=_noop_execute,
        ),
        model="test-model",
    )
    env_info = collect_env_info("test-model", cwd=tmp_path)
    return EngineContext(
        engine=engine,
        prompt_builder=SystemPromptBuilder(),
        env_info=env_info,
        lifecycle_bus=AgentEventBus(),
        model="test-model",
    )


class TestCheckpointStore:
    def test_roundtrip(self, tmp_path) -> None:
        from mini_cc.harness.events import HarnessEvent

        store = CheckpointStore(base_dir=tmp_path)
        run_state = RunState(run_id="run-1", goal="test goal")

        store.save_state(run_state)
        store.append_event(HarnessEvent(event_type="run_created", run_id="run-1", message="test goal"))
        store.append_iteration_snapshot(
            IterationSnapshot(
                run_id="run-1",
                step_id="step-1",
                step_kind=StepKind.RUN_TESTS.value,
                success=False,
                summary="1 failed",
                error="boom",
                progress_made=False,
            )
        )
        store.append_iteration_review(
            IterationReview(
                run_id="run-1",
                step_id="step-1",
                outcome=IterationOutcome.REGRESSED,
                score=IterationScore(total=-1, penalty=1),
                root_cause="boom",
            )
        )
        store.append_journal_entry("run-1", "## step-1 `run_tests`\n")
        artifact_path = store.save_artifact("run-1", "artifact.txt", "hello")
        checkpoint_path = store.save_checkpoint(run_state, "step-1")

        restored = store.load_state("run-1")
        events = store.load_events("run-1")
        snapshots = store.load_iteration_snapshots("run-1")
        reviews = store.load_iteration_reviews("run-1")

        assert restored.goal == "test goal"
        assert len(events) == 1
        assert events[0].event_type == "run_created"
        assert snapshots[0].error == "boom"
        assert reviews[0].outcome == IterationOutcome.REGRESSED
        assert store.journal_path("run-1").read_text(encoding="utf-8").startswith("## step-1")
        assert artifact_path.endswith("artifact.txt")
        assert checkpoint_path.endswith("step-1.json")

    def test_save_documentation(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)

        path = store.save_documentation("run-1", "# Run run-1 Documentation\n")

        assert path.name == "Documentation.md"
        assert path.read_text(encoding="utf-8").startswith("# Run run-1")


class TestRunDocGenerator:
    def test_generate_formats_basic_times_in_local_timezone(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        run_state = RunState(
            run_id="run-time",
            goal="check time formatting",
            created_at="2026-04-17T02:33:25+00:00",
            updated_at="2026-04-17T02:49:33+00:00",
        )
        store.save_state(run_state)

        doc = RunDocGenerator().generate(run_state, store)

        assert f"| 创建时间 | {format_local_time(run_state.created_at)} |" in doc
        assert f"| 结束时间 | {format_local_time(run_state.updated_at)} |" in doc

    def test_generate_includes_lessons_sections(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        run_state = RunState(run_id="run-doc", goal="fix tests")
        run_state.steps = [
            Step(
                id="step-1",
                kind=StepKind.RUN_TESTS,
                title="Tests",
                goal="run tests",
                status=StepStatus.FAILED_TERMINAL,
                summary="1 failed, 2 passed",
            )
        ]
        store.save_state(run_state)
        store.append_event(HarnessEvent(event_type="run_failed", run_id="run-doc", message="pytest failed"))
        store.append_iteration_snapshot(
            IterationSnapshot(
                run_id="run-doc",
                step_id="step-1",
                step_kind=StepKind.RUN_TESTS.value,
                success=False,
                summary="1 failed, 2 passed",
                error="pytest failed",
                progress_made=False,
                command="uv run pytest",
            )
        )
        store.append_iteration_review(
            IterationReview(
                run_id="run-doc",
                step_id="step-1",
                outcome=IterationOutcome.REGRESSED,
                score=IterationScore(total=-1, penalty=1),
                root_cause="pytest failed",
                useful_actions=["Keep the verification command stable between iterations"],
                next_constraints=["Reduce failing tests below 1 before finalizing"],
            )
        )

        doc = RunDocGenerator().generate(run_state, store)

        assert "## 经验教训" in doc
        assert "### 项目知识" in doc
        assert "### 失败教训" in doc
        assert "### 有效策略" in doc
        assert "`uv run pytest`" in doc
        assert "pytest failed" in doc

    def test_generate_renders_structured_decisions_and_agent_metrics(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        run_state = RunState(run_id="run-metrics", goal="inspect metrics")
        run_state.spawned_agents.append(
            AgentTrace(
                agent_id="agent-1",
                readonly=True,
                success=False,
                termination_reason="invalidated_on_resume",
                invalidated_on_resume=True,
                is_stale=True,
                output_preview="stale readonly summary",
            )
        )
        run_state.metadata["agents_created_readonly"] = "2"
        run_state.metadata["agents_created_write"] = "1"
        run_state.metadata["agents_succeeded"] = "2"
        run_state.metadata["agents_failed"] = "1"
        run_state.metadata["agents_stale"] = "1"
        run_state.metadata["agents_cancelled"] = "0"
        run_state.metadata["agent_peak_active"] = "3"
        store.save_state(run_state)
        store.append_event(
            HarnessEvent(
                event_type="run_resumed",
                run_id="run-metrics",
                message="invalidated 1 inflight agents; inserted replanning step",
                data={"decision": "resume_replan", "invalidated_agents": "1"},
            )
        )
        store.append_event(
            HarnessEvent(
                event_type="step_completed",
                run_id="run-metrics",
                step_id="step-1",
                message="done",
                data={
                    "decision": "replan",
                    "decision_reason": "verification failed; gather diagnostics and replan",
                    "active_agents": "3",
                    "inserted_steps": "inspect_failures,make_plan",
                },
            )
        )

        doc = RunDocGenerator().generate(run_state, store)

        assert "| 活跃 Agent 峰值 | 3 |" in doc
        assert "| Stale / Cancelled | 1 / 0 |" in doc
        assert "| step-1 | replan | verification failed; gather diagnostics and replan" in doc
        assert "inspect_failures,make_plan" in doc
        assert "invalidated_on_resume" in doc
        assert "stale readonly summary" in doc
        assert "| run_resumed | resume_replan | invalidated 1 inflight agents; inserted replanning step" in doc

    def test_generate_renders_task_audit_section(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        run_state = RunState(run_id="run-audit", goal="build mini jq", metadata={"audit_profile": "mini_jq"})
        artifact_path = store.save_artifact(
            "run-audit",
            "jq_audit.json",
            (
                '{"profile":"mini_jq","summary":{"cases_total":12,"cases_passed":9,"cases_failed":3},'
                '"coverage":{"identity":true,"array_iterator":false},'
                '"blockers":["array iterator syntax missing"],'
                '"improvements":["field access parity is stable"],'
                '"recommended_next_focus":"parser_and_evaluator"}'
            ),
        )
        store.append_iteration_snapshot(
            IterationSnapshot(
                run_id="run-audit",
                step_id="step-audit",
                step_kind=StepKind.RUN_TASK_AUDIT.value,
                success=True,
                summary="9/12 semantic cases passed",
                progress_made=True,
                artifact_paths={"task_audit": artifact_path},
                metadata={
                    "audit_profile": "mini_jq",
                    "audit_artifact_path": artifact_path,
                },
            )
        )

        doc = RunDocGenerator().generate(run_state, store)

        assert "## 任务专项审计" in doc
        assert "| Profile | mini_jq |" in doc
        assert "9/12 semantic cases passed" in doc
        assert "array iterator syntax missing" in doc


class TestRunHarness:
    def test_create_default_reuses_engine_context_lifecycle_bus(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)

        harness = RunHarness.create_default(engine_ctx=engine_ctx)

        assert harness._lifecycle_bus is engine_ctx.lifecycle_bus

    async def test_run_retries_and_completes(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        attempts = {"tests": 0}

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            if step.kind == StepKind.MAKE_PLAN:
                return StepResult(
                    success=True,
                    summary="plan ready",
                    progress_made=True,
                    next_steps=[
                        Step(kind=StepKind.EDIT_CODE, title="Edit", goal="edit code"),
                        Step(
                            kind=StepKind.RUN_TESTS,
                            title="Test",
                            goal="run tests",
                            inputs={"command": "echo failing"},
                        ),
                        Step(kind=StepKind.FINALIZE, title="Finalize", goal="finalize run"),
                    ],
                )
            if step.kind == StepKind.EDIT_CODE:
                return StepResult(success=True, summary="edited", progress_made=True)
            if step.kind == StepKind.RUN_TESTS:
                attempts["tests"] += 1
                if attempts["tests"] == 1:
                    return StepResult(
                        success=False,
                        summary="failed",
                        error="boom",
                        retryable=True,
                        progress_made=False,
                    )
                return StepResult(success=True, summary="tests passed", progress_made=True)
            if step.kind == StepKind.FINALIZE:
                return StepResult(success=True, summary="done", progress_made=True)
            return StepResult(success=False, summary="", retryable=False, error="unexpected step")

        runner = StepRunner(handlers={kind: _handler for kind in StepKind})
        harness = RunHarness(store=store, step_runner=runner)

        result = await harness.run(
            "complete the task",
            steps=[Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="make plan")],
        )

        restored = store.load_state(result.run_id)
        run_test_step = next(step for step in restored.steps if step.kind == StepKind.RUN_TESTS)
        documentation = store.documentation_path(result.run_id).read_text(encoding="utf-8")
        events = store.load_events(result.run_id)

        assert result.status == restored.status
        assert restored.status.value == "completed"
        assert run_test_step.retry_count == 1
        assert attempts["tests"] == 2
        assert "# Run " in documentation
        assert "## 基本信息" in documentation
        step_completed = next(event for event in events if event.event_type == "step_completed")
        assert step_completed.data["decision"] in {"continue", "retry", "complete"}
        assert "decision_reason" in step_completed.data
        assert "active_agents" in step_completed.data

    async def test_resume_existing_run(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            return StepResult(success=True, summary=f"handled:{step.kind.value}", progress_made=True)

        runner = StepRunner(handlers={StepKind.FINALIZE: _handler})
        harness = RunHarness(store=store, step_runner=runner)

        created = harness.create_run(
            "finish later",
            steps=[Step(kind=StepKind.FINALIZE, title="Finalize", goal="finalize run")],
        )
        resumed = await harness.resume(created.run_id)

        assert resumed.status.value == "completed"

    async def test_resume_marks_inflight_agents_failed(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            return StepResult(success=True, summary="done", progress_made=True)

        runner = StepRunner(handlers={StepKind.FINALIZE: _handler})
        harness = RunHarness(store=store, step_runner=runner)

        created = harness.create_run(
            "finish later",
            steps=[Step(kind=StepKind.FINALIZE, title="Finalize", goal="finalize run")],
        )
        created.spawned_agents.append(AgentTrace(agent_id="agent-1", readonly=True))
        store.save_state(created)

        resumed = await harness.resume(created.run_id)
        events = store.load_events(created.run_id)

        assert resumed.spawned_agents[0].completed_at is not None
        assert resumed.spawned_agents[0].completed_at != created.updated_at
        assert resumed.spawned_agents[0].success is False
        assert resumed.spawned_agents[0].termination_reason == "invalidated_on_resume"
        assert resumed.spawned_agents[0].invalidated_on_resume is True
        resumed_event = next(event for event in events if event.event_type == "run_resumed")
        assert resumed_event.data["decision"] == "resume_replan"
        assert "decision_reason" in resumed_event.data
        assert resumed_event.data["invalidated_agent_ids"] == "agent-1"

    async def test_resume_requeues_in_progress_step_and_inserts_replan(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        handled_steps: list[str] = []
        recovered_statuses: list[tuple[str, str | None]] = []

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            handled_steps.append(step.title)
            if step.kind == StepKind.EDIT_CODE:
                recovered_statuses.append((step.status.value, step.error))
            if step.title == "Resume Replan":
                assert "Recovered interrupted steps: step-edit, step-test." in str(step.inputs.get("prompt"))
            return StepResult(success=True, summary=f"handled:{step.title}", progress_made=True)

        runner = StepRunner(
            handlers={
                StepKind.MAKE_PLAN: _handler,
                StepKind.EDIT_CODE: _handler,
                StepKind.RUN_TESTS: _handler,
                StepKind.FINALIZE: _handler,
            }
        )
        harness = RunHarness(store=store, step_runner=runner)

        created = harness.create_run(
            "resume with replan",
            steps=[
                Step(
                    id="step-edit",
                    kind=StepKind.EDIT_CODE,
                    title="Execute",
                    goal="edit code",
                    status=StepStatus.IN_PROGRESS,
                ),
                Step(
                    id="step-test",
                    kind=StepKind.RUN_TESTS,
                    title="Verify",
                    goal="verify",
                    status=StepStatus.IN_PROGRESS,
                ),
                Step(id="step-final", kind=StepKind.FINALIZE, title="Finalize", goal="finalize"),
            ],
        )
        created.current_step_id = "step-edit"
        created.spawned_agents.append(AgentTrace(agent_id="agent-1", readonly=True))
        store.save_state(created)

        resumed = await harness.resume(created.run_id)
        events = store.load_events(created.run_id)

        assert recovered_statuses == [("in_progress", "step interrupted before completion; recovered during resume")]
        assert handled_steps.index("Resume Replan") < handled_steps.index("Execute")
        assert resumed.spawned_agents[0].invalidated_on_resume is True
        resumed_event = next(event for event in events if event.event_type == "run_resumed")
        assert resumed_event.data["invalidated_agents"] == "1"
        assert resumed_event.data["recovered_steps"] == "2"
        assert resumed_event.data["recovered_step_ids"] == "step-edit,step-test"

    async def test_failed_tests_generate_inspection_and_journal(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        attempts = {"tests": 0}

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            if step.kind == StepKind.RUN_TESTS:
                attempts["tests"] += 1
                return StepResult(
                    success=False,
                    summary="1 failed, 3 passed",
                    error="命令退出码: 1",
                    retryable=False,
                    progress_made=False,
                    metadata={"command": "uv run pytest"},
                )
            if step.kind == StepKind.INSPECT_FAILURES:
                return StepResult(success=True, summary="captured first failure", progress_made=True)
            if step.kind == StepKind.MAKE_PLAN:
                return StepResult(success=True, summary="replanned", progress_made=True)
            if step.kind == StepKind.FINALIZE:
                return StepResult(success=True, summary="done", progress_made=True)
            return StepResult(success=False, summary="", retryable=False, error="unexpected step")

        runner = StepRunner(
            handlers={
                StepKind.RUN_TESTS: _handler,
                StepKind.INSPECT_FAILURES: _handler,
                StepKind.MAKE_PLAN: _handler,
                StepKind.FINALIZE: _handler,
            }
        )
        harness = RunHarness(store=store, step_runner=runner)

        result = await harness.run(
            "debug failing tests",
            steps=[
                Step(kind=StepKind.RUN_TESTS, title="Tests", goal="run tests"),
                Step(kind=StepKind.FINALIZE, title="Finalize", goal="finalize"),
            ],
        )

        restored = store.load_state(result.run_id)
        inspect_step = next(step for step in restored.steps if step.kind == StepKind.INSPECT_FAILURES)
        reviews = store.load_iteration_reviews(result.run_id)
        journal = store.journal_path(result.run_id).read_text(encoding="utf-8")

        assert restored.status.value == "completed"
        assert inspect_step.inputs["command"] == "uv run pytest -x -vv"
        assert "Reduce failing tests below 1 before finalizing" in inspect_step.goal
        assert "Address this error directly: 命令退出码: 1" in inspect_step.goal
        assert reviews[0].recommended_step_kind == StepKind.INSPECT_FAILURES.value
        assert "Inspect Failures" in journal
        assert attempts["tests"] == 1

    async def test_cancel_interrupts_inflight_query_step(self, tmp_path) -> None:
        interrupt_seen = {"value": False}
        engine_ctx = _make_engine_ctx(tmp_path)

        async def _slow_stream(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
            while not engine_ctx.is_interrupted:
                await asyncio.sleep(0.02)
            interrupt_seen["value"] = True
            return
            yield

        engine_ctx.engine = QueryEngine(
            stream_fn=_slow_stream,
            tool_use_ctx=ToolUseContext(
                get_schemas=lambda: [],
                execute=_noop_execute,
                is_interrupted=lambda: engine_ctx.is_interrupted,
            ),
            model="test-model",
        )
        harness = RunHarness.create_default(engine_ctx=engine_ctx, store=CheckpointStore(base_dir=tmp_path))

        run_task = asyncio.create_task(
            harness.run(
                "cancel query",
                steps=[Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="plan")],
            )
        )
        await asyncio.sleep(0.1)
        run_id = next(iter(harness._run_interrupts))
        harness.cancel(run_id)
        result = await asyncio.wait_for(run_task, timeout=2.0)

        assert interrupt_seen["value"] is True
        assert result.status == RunStatus.CANCELLED
        assert result.steps[0].status == StepStatus.FAILED_TERMINAL


class TestIterationOptimizer:
    def test_apply_constraints_to_generated_steps_updates_goal_and_prompt(self) -> None:
        optimizer = IterationOptimizer()
        review = IterationReview(
            run_id="run-1",
            step_id="step-1",
            outcome=IterationOutcome.REGRESSED,
            score=IterationScore(total=-1, penalty=1),
            root_cause="pytest failed",
            next_constraints=[
                "Reduce failing tests before finalizing",
                "Address this error directly: 命令退出码: 1",
            ],
        )
        steps = [
            Step(kind=StepKind.MAKE_PLAN, title="Replan", goal="Generate a revised plan."),
            Step(
                kind=StepKind.EDIT_CODE,
                title="Fix",
                goal="Fix the code",
                inputs={"prompt": "Fix the code paths related to the failure."},
            ),
            Step(kind=StepKind.RUN_TESTS, title="Verify", goal="Run tests", inputs={"command": "uv run pytest"}),
        ]

        updated = optimizer.apply_constraints_to_steps(steps, review)

        assert "Constraints:" in updated[0].goal
        assert "Constraints:" in updated[0].inputs["prompt"]
        assert "Address this error directly: 命令退出码: 1" in updated[1].inputs["prompt"]
        assert "Constraints:" in updated[2].goal
        assert "prompt" not in updated[2].inputs

    def test_run_tests_generate_task_audit_step_when_profile_enabled(self) -> None:
        optimizer = IterationOptimizer()
        run_state = RunState(run_id="run-audit-step", goal="mini jq", metadata={"audit_profile": "mini_jq"})
        step = Step(kind=StepKind.RUN_TESTS, title="Tests", goal="run tests")
        result = StepResult(success=True, summary="12 passed", progress_made=True)
        review = IterationReview(
            run_id="run-audit-step",
            step_id="step-tests",
            outcome=IterationOutcome.IMPROVED,
            score=IterationScore(total=5),
            root_cause="ok",
        )

        generated = optimizer.apply_review(run_state, step, result, review)

        audit_step = next(item for item in generated if item.kind == StepKind.RUN_TASK_AUDIT)
        assert audit_step.inputs["profile"] == "mini_jq"
        assert audit_step.inputs["artifact_name"] == "jq_audit.json"

    def test_capture_reads_task_audit_artifact_metadata(self, tmp_path) -> None:
        optimizer = IterationOptimizer()
        artifact_path = tmp_path / "jq_audit.json"
        artifact_path.write_text(
            (
                '{"profile":"mini_jq","summary":{"cases_total":20,"cases_passed":15,"cases_failed":5},'
                '"coverage":{"identity":true,"pipe":"partial"},'
                '"blockers":["pipe evaluator mismatch"],'
                '"recommended_next_focus":"pipe_semantics"}'
            ),
            encoding="utf-8",
        )
        run_state = RunState(run_id="run-capture", goal="mini jq", metadata={"audit_profile": "mini_jq"})
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(success=True, summary="audit done", progress_made=True)

        snapshot = optimizer.capture(run_state, step, result, {"task_audit": str(artifact_path)})

        assert snapshot.metadata["audit_profile"] == "mini_jq"
        assert snapshot.metadata["audit_summary"] == "15/20 semantic cases passed"
        assert snapshot.metadata["audit_cases_passed"] == "15"
        assert snapshot.metadata["audit_blockers"] == "pipe evaluator mismatch"
        assert snapshot.metadata["audit_next_focus"] == "pipe_semantics"

    def test_capture_includes_generic_agent_issue_metadata(self) -> None:
        optimizer = IterationOptimizer()
        run_state = RunState(
            run_id="run-agent-meta",
            goal="test",
            metadata={
                "agent_latest_issue": "agent-1 returned stale results",
                "agents_failed": "1",
                "agents_stale": "1",
                "agents_cancelled": "0",
            },
        )
        step = Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="plan")
        result = StepResult(success=True, summary="planned", progress_made=True)

        snapshot = optimizer.capture(run_state, step, result, {})

        assert snapshot.metadata["agent_latest_issue"] == "agent-1 returned stale results"
        assert snapshot.metadata["agents_failed"] == "1"
        assert snapshot.metadata["agents_stale"] == "1"

    def test_review_regresses_when_generic_agent_issue_appears(self) -> None:
        optimizer = IterationOptimizer()
        current = IterationSnapshot(
            run_id="run-review",
            step_id="step-1",
            step_kind=StepKind.EDIT_CODE.value,
            success=True,
            summary="edited",
            progress_made=False,
            metadata={
                "agent_latest_issue": "agent-1 returned stale results",
                "agents_failed": "1",
                "agents_stale": "1",
            },
        )

        review = optimizer.review(current, previous=None)

        assert review.outcome == IterationOutcome.REGRESSED
        assert review.root_cause == "Sub-agent issue: agent-1 returned stale results"
        assert any("Resolve sub-agent issue" in item for item in review.next_constraints)
        assert review.recommended_step_kind == StepKind.MAKE_PLAN.value

    def test_review_blocks_when_same_agent_issue_repeats(self) -> None:
        optimizer = IterationOptimizer()
        previous = IterationSnapshot(
            run_id="run-review",
            step_id="step-0",
            step_kind=StepKind.MAKE_PLAN.value,
            success=True,
            summary="planned",
            progress_made=False,
            metadata={"agent_latest_issue": "agent-1 returned stale results"},
        )
        current = IterationSnapshot(
            run_id="run-review",
            step_id="step-1",
            step_kind=StepKind.MAKE_PLAN.value,
            success=True,
            summary="planned again",
            progress_made=False,
            metadata={"agent_latest_issue": "agent-1 returned stale results"},
        )

        review = optimizer.review(current, previous=previous)

        assert review.outcome == IterationOutcome.BLOCKED


class TestStepRunnerQueryIntegration:
    async def test_analyze_step_delegates_to_readonly_agent_when_manager_available(self, tmp_path) -> None:
        class _FakeAgent:
            def __init__(self) -> None:
                self.config = type("Config", (), {"agent_id": "agent1234"})()
                self.task_id = 7

            async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
                yield TextDelta(content="delegated analysis")

        class _FakeManager:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def create_agent(
                self,
                *,
                prompt: str,
                readonly: bool = False,
                fork: bool = False,
                parent_state=None,
                mode: str = "build",
                scope_paths: list[str] | None = None,
                run_id: str | None = None,
            ) -> _FakeAgent:
                self.calls.append(
                    {
                        "prompt": prompt,
                        "readonly": readonly,
                        "fork": fork,
                        "parent_state": parent_state,
                        "mode": mode,
                        "scope_paths": scope_paths,
                        "run_id": run_id,
                    }
                )
                return _FakeAgent()

        engine_ctx = _make_engine_ctx(tmp_path)
        fake_manager = _FakeManager()
        engine_ctx.agent_manager = fake_manager  # type: ignore[assignment]
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(run_id="run-delegate-ro", goal="test")
        step = Step(
            kind=StepKind.ANALYZE_REPO,
            title="Analyze",
            goal="analyze repo",
            inputs={"prompt": "inspect repo"},
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert result.summary == "delegated analysis"
        assert result.metadata["delegated_agent_id"] == "agent1234"
        assert result.metadata["delegated_agent_readonly"] == "true"
        assert fake_manager.calls == [
            {
                "prompt": "inspect repo",
                "readonly": True,
                "fork": False,
                "parent_state": None,
                "mode": "plan",
                "scope_paths": [],
                "run_id": "run-delegate-ro",
            }
        ]

    async def test_edit_step_delegates_to_write_agent_when_manager_available(self, tmp_path) -> None:
        class _FakeAgent:
            def __init__(self) -> None:
                self.config = type("Config", (), {"agent_id": "writer123"})()
                self.task_id = 9

            async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
                yield TextDelta(content="delegated edit")

        class _FakeManager:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def create_agent(
                self,
                *,
                prompt: str,
                readonly: bool = False,
                fork: bool = False,
                parent_state=None,
                mode: str = "build",
                scope_paths: list[str] | None = None,
                run_id: str | None = None,
            ) -> _FakeAgent:
                self.calls.append(
                    {
                        "prompt": prompt,
                        "readonly": readonly,
                        "fork": fork,
                        "parent_state": parent_state,
                        "mode": mode,
                        "scope_paths": scope_paths,
                        "run_id": run_id,
                    }
                )
                return _FakeAgent()

        engine_ctx = _make_engine_ctx(tmp_path)
        fake_manager = _FakeManager()
        engine_ctx.agent_manager = fake_manager  # type: ignore[assignment]
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(run_id="run-delegate-write", goal="test")
        step = Step(
            kind=StepKind.EDIT_CODE,
            title="Execute",
            goal="edit code",
            inputs={"prompt": "implement feature"},
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert result.summary == "delegated edit"
        assert result.metadata["delegated_agent_id"] == "writer123"
        assert result.metadata["delegated_agent_readonly"] == "false"
        assert fake_manager.calls == [
            {
                "prompt": "implement feature",
                "readonly": False,
                "fork": False,
                "parent_state": None,
                "mode": "build",
                "scope_paths": ["."],
                "run_id": "run-delegate-write",
            }
        ]

    async def test_query_backed_step_uses_engine_context(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        engine_ctx.agent_manager = None
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(run_id="run-query", goal="test")
        step = Step(
            kind=StepKind.MAKE_PLAN,
            title="Plan",
            goal="make plan",
            inputs={"prompt": "inspect src"},
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert result.summary == "handled:inspect src"
        assert result.query_state is not None
        assert result.query_state.messages[0].role == Role.SYSTEM
        assert result.query_state.turn_count == 1
        assert result.query_state.messages[-1].role == Role.ASSISTANT

    async def test_query_step_times_out_using_default_budget(self, tmp_path) -> None:
        async def _slow_stream(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
            await asyncio.sleep(1.05)
            yield TextDelta(content="late")

        engine = QueryEngine(
            stream_fn=_slow_stream,
            tool_use_ctx=ToolUseContext(
                get_schemas=lambda: [],
                execute=_noop_execute,
            ),
            model="test-model",
        )
        env_info = collect_env_info("test-model", cwd=tmp_path)
        engine_ctx = EngineContext(
            engine=engine,
            prompt_builder=SystemPromptBuilder(),
            env_info=env_info,
            model="test-model",
        )
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(
            run_id="run-query-timeout",
            goal="test",
            budget=RunBudget(max_step_seconds=1),
        )
        step = Step(
            kind=StepKind.ANALYZE_REPO,
            title="Analyze",
            goal="analyze repository",
            inputs={"prompt": "inspect src"},
            budget_seconds=0,
        )

        result = await runner.run_step(step, run_state)

        assert result.success is False
        assert "Step timed out after 1s" in result.error
        assert "LLM provider never returned any event" in result.error
        assert result.metadata["timeout_seconds"] == "1"
        assert result.metadata["diag_last_event_type"] == "(none)"
        assert result.metadata["diag_message_count"] == "1"
        assert engine_ctx.mode == "build"

    async def test_bash_step_timeout_is_capped_by_step_budget(self) -> None:
        class _RecordingBash:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []

            def execute(self, *, command: str, timeout: int):
                from mini_cc.tools.base import ToolResult

                self.calls.append((command, timeout))
                return ToolResult(output="ok", success=True)

        bash = _RecordingBash()
        runner = StepRunner(bash_tool=bash)  # type: ignore[arg-type]
        run_state = RunState(run_id="run-bash-timeout", goal="test", budget=RunBudget(max_step_seconds=30))
        step = Step(
            kind=StepKind.RUN_TESTS,
            title="Tests",
            goal="run tests",
            inputs={"command": "pytest", "timeout": 120000},
            budget_seconds=2,
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert bash.calls == [("pytest", 2000)]

    def test_query_diagnostics_collects_tool_trace_metadata(self) -> None:
        diag = _QueryDiagnostics()

        diag.record_event(TextDelta(content="working"))
        diag.record_event(ToolCallStart(tool_call_id="tc_agent", name="agent"))
        diag.record_event(
            ToolCallDelta(
                tool_call_id="tc_agent",
                arguments_json_delta='{"prompt":"inspect src","readonly":true}',
            )
        )
        diag.record_event(ToolCallEnd(tool_call_id="tc_agent"))
        diag.record_event(AgentStartEvent(agent_id="agent-12345678", task_id=1, prompt="inspect src"))
        diag.record_event(AgentToolCallEvent(agent_id="agent-12345678", tool_name="file_read"))
        diag.record_event(
            AgentToolResultEvent(
                agent_id="agent-12345678",
                tool_name="file_read",
                success=True,
                output_preview="ok",
            )
        )
        diag.record_event(
            AgentCompletionEvent(
                agent_id="agent-12345678",
                task_id=1,
                success=True,
                output="done",
                output_path="/tmp/agent.output",
            )
        )
        diag.record_event(ToolResultEvent(tool_call_id="tc_agent", name="agent", output="done", success=True))

        metadata = diag.to_metadata()

        assert "diag_tool_trace" in metadata
        assert "agent(inspect src)=" in metadata["diag_tool_trace"]
        assert "agent[agent-12].file_read(success=true" in metadata["diag_tool_trace"]


class TestSupervisorMetadata:
    async def test_step_completed_event_includes_result_metadata(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            return StepResult(
                success=True,
                summary="ok",
                progress_made=True,
                metadata={"diag_tool_trace": "agent(readonly=true)=1.2s"},
            )

        runner = StepRunner(handlers={StepKind.ANALYZE_REPO: _handler})
        supervisor = SupervisorLoop(store=store, step_runner=runner)
        run_state = RunState(
            run_id="run-event-meta",
            goal="inspect metadata",
            steps=[Step(id="step-1", kind=StepKind.ANALYZE_REPO, title="Analyze", goal="analyze")],
        )
        store.save_state(run_state)

        await supervisor.run(run_state)

        events = store.load_events(run_state.run_id)
        step_completed = next(event for event in events if event.event_type == "step_completed")
        assert step_completed.data["diag_tool_trace"] == "agent(readonly=true)=1.2s"


class TestAgentBudget:
    def test_default_budget(self) -> None:
        budget = AgentBudget()
        assert budget.remaining_readonly == 5
        assert budget.remaining_write == 1

    def test_deduction_readonly(self) -> None:
        budget = AgentBudget(remaining_readonly=3)
        budget.remaining_readonly -= 1
        assert budget.remaining_readonly == 2

    def test_deduction_write(self) -> None:
        budget = AgentBudget(remaining_write=1)
        budget.remaining_write -= 1
        assert budget.remaining_write == 0

    def test_exhaustion_returns_error(self) -> None:
        budget = AgentBudget(remaining_readonly=0, remaining_write=0)
        assert budget.remaining_readonly <= 0
        assert budget.remaining_write <= 0


class TestAgentTraceAndActiveCount:
    def test_active_agent_count_with_no_agents(self) -> None:
        state = RunState(run_id="r1", goal="test")
        assert state.active_agent_count == 0

    def test_blocked_is_terminal(self) -> None:
        state = RunState(run_id="r1", goal="test", status="blocked")
        assert state.is_terminal is True

    def test_terminal_limit_decision_preserves_blocked_phase(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)
        supervisor = SupervisorLoop(store=store, step_runner=StepRunner())
        run_state = RunState(run_id="r1", goal="test")

        supervisor._apply_terminal_decision(
            run_state,
            PolicyDecision(
                action=PolicyAction.BLOCK,
                reason="active agent limit exceeded",
                terminal_status="blocked",
            ),
        )

        assert run_state.status.value == "blocked"
        assert run_state.phase == "blocked"

    def test_active_agent_count_with_active_agents(self) -> None:
        state = RunState(
            run_id="r1",
            goal="test",
            spawned_agents=[
                AgentTrace(agent_id="a1", readonly=True),
                AgentTrace(agent_id="a2", readonly=True),
            ],
        )
        assert state.active_agent_count == 2

    def test_active_agent_count_excludes_completed(self) -> None:
        state = RunState(
            run_id="r1",
            goal="test",
            spawned_agents=[
                AgentTrace(agent_id="a1", readonly=True),
                AgentTrace(agent_id="a2", readonly=True, completed_at="2026-01-01T00:00:00Z", success=True),
            ],
        )
        assert state.active_agent_count == 1

    def test_active_write_agent_count(self) -> None:
        state = RunState(
            run_id="r1",
            goal="test",
            spawned_agents=[
                AgentTrace(agent_id="ro-1", readonly=True),
                AgentTrace(agent_id="wr-1", readonly=False),
                AgentTrace(agent_id="wr-2", readonly=False, completed_at="2026-01-01T00:00:00Z", success=True),
            ],
        )
        assert state.active_write_agent_count == 1
        assert state.active_readonly_agent_count == 1


class TestSupervisorLifecycleDrain:
    async def test_lifecycle_events_update_spawned_agents(self, tmp_path) -> None:
        from mini_cc.agent.bus import AgentEventBus, AgentLifecycleEvent

        store = CheckpointStore(base_dir=tmp_path)
        bus = AgentEventBus()

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            if step.kind == StepKind.FINALIZE:
                return StepResult(success=True, summary="done", progress_made=True)
            bus.publish_nowait(
                AgentLifecycleEvent(
                    event_type="created",
                    agent_id="agent-001",
                    source_step_id=step.id,
                    readonly=True,
                    scope_paths=["src/"],
                )
            )
            return StepResult(success=True, summary="plan done", progress_made=True)

        runner = StepRunner(
            handlers={
                StepKind.MAKE_PLAN: _handler,
                StepKind.FINALIZE: _handler,
            }
        )
        harness = RunHarness(store=store, step_runner=runner, lifecycle_bus=bus)

        result = await harness.run(
            "test lifecycle",
            steps=[
                Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="plan"),
                Step(kind=StepKind.FINALIZE, title="Finalize", goal="finalize"),
            ],
        )

        assert result.spawned_agents
        assert result.spawned_agents[0].agent_id == "agent-001"
        assert result.spawned_agents[0].readonly is True
        assert result.spawned_agents[0].source_step_id is not None
        assert result.metadata["agents_created_readonly"] == "1"
        assert result.metadata["agent_peak_active"] == "1"

    async def test_lifecycle_completion_captures_generic_agent_result_signals(self, tmp_path) -> None:
        from mini_cc.agent.bus import AgentEventBus, AgentLifecycleEvent

        store = CheckpointStore(base_dir=tmp_path)
        bus = AgentEventBus()

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            if step.kind == StepKind.FINALIZE:
                return StepResult(success=True, summary="done", progress_made=True)
            bus.publish_nowait(
                AgentLifecycleEvent(
                    event_type="created",
                    agent_id="agent-001",
                    source_step_id=step.id,
                    readonly=True,
                )
            )
            bus.publish_nowait(
                AgentLifecycleEvent(
                    event_type="completed",
                    agent_id="agent-001",
                    success=False,
                    output_preview="investigation failed due to stale workspace",
                    output_path="/tmp/agent-001.output",
                    is_stale=True,
                    base_version_stamp="base-1",
                    completed_version_stamp="completed-2",
                    termination_reason="failed to reconcile latest edits",
                )
            )
            return StepResult(success=True, summary="plan done", progress_made=True)

        runner = StepRunner(
            handlers={
                StepKind.MAKE_PLAN: _handler,
                StepKind.FINALIZE: _handler,
            }
        )
        harness = RunHarness(store=store, step_runner=runner, lifecycle_bus=bus)

        result = await harness.run(
            "test lifecycle result signals",
            steps=[
                Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="plan"),
                Step(kind=StepKind.FINALIZE, title="Finalize", goal="finalize"),
            ],
        )

        trace = result.spawned_agents[0]
        assert trace.success is False
        assert trace.is_stale is True
        assert trace.output_preview == "investigation failed due to stale workspace"
        assert trace.termination_reason == "failed to reconcile latest edits"
        assert result.metadata["agents_stale"] == "1"
        assert "stale results" in result.metadata["agent_latest_issue"]

    async def test_run_waits_for_active_agents_before_completing(self, tmp_path) -> None:
        from mini_cc.agent.bus import AgentLifecycleEvent

        store = CheckpointStore(base_dir=tmp_path)

        class _DeterministicBus:
            def __init__(self) -> None:
                self._drain_count = 0

            def drain(self) -> list[AgentLifecycleEvent]:
                self._drain_count += 1
                if self._drain_count == 2:
                    return [
                        AgentLifecycleEvent(
                            event_type="created",
                            agent_id="agent-001",
                            source_step_id="",
                            readonly=True,
                        )
                    ]
                if self._drain_count == 4:
                    return [
                        AgentLifecycleEvent(
                            event_type="completed",
                            agent_id="agent-001",
                            success=True,
                        )
                    ]
                return []

        bus = _DeterministicBus()

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            return StepResult(success=True, summary="plan done", progress_made=True)

        runner = StepRunner(handlers={StepKind.MAKE_PLAN: _handler})
        harness = RunHarness(store=store, step_runner=runner, lifecycle_bus=bus)

        result = await harness.run(
            "test wait for agents",
            steps=[Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="plan")],
        )

        assert result.status.value == "completed"
        assert result.active_agent_count == 0
        assert result.spawned_agents[0].completed_at is not None


class TestSupervisorStepExceptions:
    async def test_unhandled_step_exception_becomes_terminal_run_state(self, tmp_path) -> None:
        store = CheckpointStore(base_dir=tmp_path)

        async def _handler(step: Step, run_state: RunState) -> StepResult:
            raise RuntimeError("boom")

        runner = StepRunner(handlers={StepKind.MAKE_PLAN: _handler})
        harness = RunHarness(store=store, step_runner=runner)

        result = await harness.run(
            "step exception",
            steps=[Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="plan")],
        )

        restored = store.load_state(result.run_id)
        step = restored.steps[0]

        assert result.status.value == "blocked"
        assert step.status == StepStatus.FAILED_TERMINAL
        assert step.error == "Unhandled step exception: boom"


class TestAgentBudgetInStepRunner:
    async def test_query_step_injects_budget(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(run_id="run-budget", goal="test")
        step = Step(
            kind=StepKind.ANALYZE_REPO,
            title="Analyze",
            goal="analyze repo",
            inputs={"prompt": "look at code"},
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert "agents_remaining_ro" in result.metadata
        assert result.metadata["agents_remaining_ro"].isdigit()
        assert engine_ctx.agent_budget is not None
        assert engine_ctx.agent_budget.remaining_readonly >= 0

    async def test_query_step_uses_configured_agent_budget(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(
            run_id="run-budget-configured",
            goal="test",
            agent_budget=AgentBudget(max_readonly=7, max_write=2, remaining_readonly=7, remaining_write=2),
        )
        step = Step(
            kind=StepKind.ANALYZE_REPO,
            title="Analyze",
            goal="analyze repo",
            inputs={"prompt": "look at code"},
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert engine_ctx.agent_budget is not None
        assert engine_ctx.agent_budget.max_readonly == 7
        assert engine_ctx.agent_budget.max_write == 1
        assert engine_ctx.agent_budget.remaining_write == 1

    async def test_query_step_blocks_new_write_budget_when_write_agent_active(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(
            run_id="run-write-serial",
            goal="test",
            spawned_agents=[AgentTrace(agent_id="wr-1", readonly=False)],
        )
        step = Step(
            kind=StepKind.ANALYZE_REPO,
            title="Analyze",
            goal="analyze repo",
            inputs={"prompt": "look at code"},
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert engine_ctx.agent_budget is not None
        assert engine_ctx.agent_budget.remaining_write == 0

    async def test_task_audit_step_uses_named_json_artifact(self, tmp_path) -> None:
        runner = StepRunner()
        run_state = RunState(run_id="run-task-audit", goal="audit")
        step = Step(
            kind=StepKind.RUN_TASK_AUDIT,
            title="Task Audit",
            goal="run task audit",
            inputs={
                "command": (
                    'printf \'{"profile":"mini_jq","summary":{"cases_total":10,"cases_passed":8,"cases_failed":2},'
                    '"blockers":["pipe partial"],"regressions":[],"improvements":["field access stable"],'
                    '"recommended_next_focus":"pipe_semantics"}\''
                ),
                "profile": "mini_jq",
                "artifact_name": "jq_audit.json",
            },
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert "task_audit" in result.artifacts
        assert result.metadata["artifact_name"] == "jq_audit.json"
        assert result.metadata["audit_profile"] == "mini_jq"
        assert result.metadata["audit_summary"] == "8/10 semantic cases passed"
        assert result.metadata["audit_blockers"] == "pipe partial"
        assert result.metadata["audit_next_focus"] == "pipe_semantics"
        assert result.metadata["audit_improvements"] == "field access stable"


class TestPolicyEngine:
    def test_active_agent_limit_blocks_run(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(
            run_id="run-limit",
            goal="test",
            budget=RunBudget(max_active_agents=2),
            spawned_agents=[
                AgentTrace(agent_id="a1", readonly=True),
                AgentTrace(agent_id="a2", readonly=True),
                AgentTrace(agent_id="a3", readonly=True),
                AgentTrace(agent_id="a4", readonly=True),
                AgentTrace(agent_id="a5", readonly=True),
            ],
        )

        decision = engine.check_run_limits(run_state)

        assert decision is not None
        assert decision.action == PolicyAction.BLOCK

    def test_replan_limit_fails_instead_of_replanning(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(run_id="run-replan", goal="test", replan_count=3)
        step = Step(kind=StepKind.RUN_TESTS, title="Tests", goal="run tests")
        result = StepResult(success=False, summary="failed", retryable=False, progress_made=False)

        decision = engine.evaluate_step(run_state, step, result, RunHealth.REGRESSING)

        assert decision.action == PolicyAction.FAIL

    def test_multiple_active_write_agents_block_run(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(
            run_id="run-write-conflict",
            goal="test",
            spawned_agents=[
                AgentTrace(agent_id="wr-1", readonly=False),
                AgentTrace(agent_id="wr-2", readonly=False),
            ],
        )

        decision = engine.check_run_limits(run_state)

        assert decision is not None
        assert decision.action == PolicyAction.BLOCK

    def test_audit_failure_triggers_audit_replan(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(run_id="run-audit-fail", goal="mini jq")
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(
            success=False,
            summary="audit failed",
            retryable=False,
            progress_made=False,
            metadata={"audit_blockers": "parser missing", "audit_next_focus": "parser"},
        )

        decision = engine.evaluate_step(run_state, step, result, RunHealth.REGRESSING)

        assert decision.action == PolicyAction.REPLAN
        assert len(decision.insert_steps) == 1
        assert decision.insert_steps[0].kind == StepKind.MAKE_PLAN
        assert (
            "audit blockers" in decision.insert_steps[0].goal.lower()
            or "Resolve audit blockers" in decision.insert_steps[0].goal
        )

    def test_audit_failure_respects_replan_limit(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(run_id="run-audit-limit", goal="mini jq", replan_count=3)
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(success=False, summary="failed", retryable=False, progress_made=False)

        decision = engine.evaluate_step(run_state, step, result, RunHealth.REGRESSING)

        assert decision.action == PolicyAction.FAIL

    def test_replan_step_includes_audit_next_focus(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(
            run_id="run-focus",
            goal="mini jq",
            metadata={"audit_next_focus": "array_iterator"},
        )
        step = Step(kind=StepKind.EDIT_CODE, title="Edit", goal="edit")
        result = StepResult(success=True, summary="ok", progress_made=False)

        decision = engine.evaluate_step(run_state, step, result, RunHealth.STALLED)

        assert decision.action == PolicyAction.REPLAN
        assert "array_iterator" in decision.insert_steps[0].goal

    def test_timeout_step_retries_before_other_failure_policies(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(run_id="run-timeout-retry", goal="test")
        step = Step(kind=StepKind.EDIT_CODE, title="Edit", goal="edit")
        result = StepResult(
            success=False,
            summary="",
            retryable=True,
            timed_out=True,
            progress_made=False,
            metadata={"timeout_seconds": "5"},
        )

        decision = engine.evaluate_step(run_state, step, result, RunHealth.STALLED)

        assert decision.action == PolicyAction.RETRY
        assert decision.reason == "step timed out but retry budget remains"

    def test_timeout_verification_step_replans_after_retries_exhausted(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(run_id="run-timeout-replan", goal="test")
        step = Step(kind=StepKind.RUN_TESTS, title="Tests", goal="test", retry_count=2)
        result = StepResult(
            success=False,
            summary="",
            retryable=True,
            timed_out=True,
            progress_made=False,
            metadata={"timeout_seconds": "2"},
        )

        decision = engine.evaluate_step(run_state, step, result, RunHealth.STALLED)

        assert decision.action == PolicyAction.REPLAN
        assert decision.insert_steps[0].title == "Timeout Replan"
        assert "2 seconds" in decision.insert_steps[0].goal

    def test_repeated_timeouts_block_run(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(
            run_id="run-timeout-blocked",
            goal="test",
            failure_count=3,
        )
        step = Step(kind=StepKind.EDIT_CODE, title="Edit", goal="edit", retry_count=2)
        result = StepResult(
            success=False,
            summary="",
            retryable=True,
            timed_out=True,
            progress_made=False,
            metadata={"timeout_seconds": "3"},
        )

        decision = engine.evaluate_step(run_state, step, result, RunHealth.BLOCKED)

        assert decision.action == PolicyAction.BLOCK
        assert decision.reason == "run is blocked by repeated step timeouts"

    def test_replan_step_includes_latest_agent_issue(self) -> None:
        engine = PolicyEngine()
        run_state = RunState(
            run_id="run-agent-issue",
            goal="test",
            metadata={"agent_latest_issue": "agent-1 returned stale results"},
        )
        step = Step(kind=StepKind.EDIT_CODE, title="Edit", goal="edit")
        result = StepResult(success=True, summary="ok", progress_made=False)

        decision = engine.evaluate_step(run_state, step, result, RunHealth.STALLED)

        assert decision.action == PolicyAction.REPLAN
        assert "agent-1 returned stale results" in decision.insert_steps[0].goal


class TestRunJudgeAuditAwareness:
    def test_audit_step_progressing_when_improvements(self) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="mini jq")
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(
            success=True,
            summary="audit passed",
            progress_made=True,
            metadata={"audit_improvements": "field access stable"},
        )

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.PROGRESSING

    def test_audit_step_regressing_when_regressions(self) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="mini jq")
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(
            success=True,
            summary="audit done",
            progress_made=True,
            metadata={"audit_regressions": "identity failed"},
        )

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.REGRESSING

    def test_audit_step_blocked_when_blocker_repeats(self, tmp_path) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="mini jq", consecutive_no_progress_count=3)
        artifact_file = tmp_path / "prev_audit.json"
        artifact_file.write_text(
            json.dumps({"profile": "mini_jq", "blockers": ["parser missing case failed"]}),
            encoding="utf-8",
        )
        prev_step = Step(
            kind=StepKind.RUN_TASK_AUDIT,
            title="Audit",
            goal="audit",
            status=StepStatus.SUCCEEDED,
            artifacts={"task_audit": str(artifact_file)},
        )
        run_state.steps.append(prev_step)
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(
            success=True,
            summary="audit done",
            progress_made=False,
            metadata={"audit_blockers": "parser missing case failed"},
        )

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.BLOCKED

    def test_audit_step_stalled_when_no_change(self) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="mini jq")
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(success=True, summary="audit done", progress_made=False)

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.STALLED

    def test_audit_step_regressing_on_failure(self) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="mini jq")
        step = Step(kind=StepKind.RUN_TASK_AUDIT, title="Audit", goal="audit")
        result = StepResult(success=False, summary="audit failed", progress_made=False)

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.REGRESSING

    def test_non_audit_step_uses_standard_logic(self) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="test")
        step = Step(kind=StepKind.RUN_TESTS, title="Tests", goal="test")
        result = StepResult(success=True, summary="ok", progress_made=True)

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.PROGRESSING

    def test_timeout_step_is_stalled_before_reaching_failure_threshold(self) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="test", failure_count=1)
        step = Step(kind=StepKind.RUN_TESTS, title="Tests", goal="test")
        result = StepResult(success=False, summary="", timed_out=True, progress_made=False)

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.STALLED

    def test_timeout_step_is_blocked_after_reaching_failure_threshold(self) -> None:
        judge = RunJudge()
        run_state = RunState(run_id="r1", goal="test", failure_count=3)
        step = Step(kind=StepKind.RUN_TESTS, title="Tests", goal="test")
        result = StepResult(success=False, summary="", timed_out=True, progress_made=False)

        health = judge.assess(run_state, step, result)

        assert health == RunHealth.BLOCKED


class TestTaskAuditRegistryResolve:
    def test_resolve_returns_mini_jq_profile(self) -> None:
        registry = TaskAuditRegistry()
        profile = registry.resolve_for_run({"audit_profile": "mini_jq"})
        assert profile is not None
        assert profile.profile_id == "mini_jq"

    def test_resolve_returns_none_without_profile(self) -> None:
        registry = TaskAuditRegistry()
        profile = registry.resolve_for_run({})
        assert profile is None

    def test_build_audit_step_creates_run_task_audit_step(self) -> None:
        registry = TaskAuditRegistry()
        step = registry.build_audit_step({"audit_profile": "mini_jq"}, "uv run pytest")
        assert step is not None
        assert step.kind == StepKind.RUN_TASK_AUDIT
        assert step.inputs["profile"] == "mini_jq"
        assert step.inputs["artifact_name"] == "jq_audit.json"

    def test_registry_loads_filesystem_plugin(self, tmp_path) -> None:
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        plugin_file = plugin_dir / "demo_plugin.py"
        plugin_file.write_text(
            (
                "from __future__ import annotations\n"
                "from mini_cc.harness.task_audit import TaskAuditProfile, TaskAuditResult\n"
                "\n"
                "class DemoProfile(TaskAuditProfile):\n"
                "    profile_id = 'demo_task'\n"
                "    display_name = 'Demo Task'\n"
                "    artifact_name = 'demo_audit.json'\n"
                "\n"
                "    def parse_result(self, artifact_path: str) -> TaskAuditResult | None:\n"
                "        return TaskAuditResult(profile_id=self.profile_id, summary='demo')\n"
                "\n"
                "def register() -> TaskAuditProfile:\n"
                "    return DemoProfile()\n"
            ),
            encoding="utf-8",
        )

        registry = TaskAuditRegistry(plugin_paths=[plugin_dir])
        profile = registry.get("demo_task")

        assert profile is not None
        assert profile.profile_id == "demo_task"
        assert profile.artifact_name == "demo_audit.json"

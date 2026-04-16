from __future__ import annotations

from collections.abc import AsyncGenerator

from mini_cc.context.engine_context import EngineContext
from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.harness import CheckpointStore, RunHarness, RunState, Step, StepKind, StepResult, StepRunner
from mini_cc.models import Event, Message, Role, TextDelta, ToolCall, ToolResultEvent
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
        model="test-model",
    )


class TestCheckpointStore:
    def test_roundtrip(self, tmp_path) -> None:
        from mini_cc.harness.events import HarnessEvent

        store = CheckpointStore(base_dir=tmp_path)
        run_state = RunState(run_id="run-1", goal="test goal")

        store.save_state(run_state)
        store.append_event(HarnessEvent(event_type="run_created", run_id="run-1", message="test goal"))
        artifact_path = store.save_artifact("run-1", "artifact.txt", "hello")
        checkpoint_path = store.save_checkpoint(run_state, "step-1")

        restored = store.load_state("run-1")
        events = store.load_events("run-1")

        assert restored.goal == "test goal"
        assert len(events) == 1
        assert events[0].event_type == "run_created"
        assert artifact_path.endswith("artifact.txt")
        assert checkpoint_path.endswith("step-1.json")


class TestRunHarness:
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

        assert result.status == restored.status
        assert restored.status.value == "completed"
        assert run_test_step.retry_count == 1
        assert attempts["tests"] == 2

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


class TestStepRunnerQueryIntegration:
    async def test_query_backed_step_uses_engine_context(self, tmp_path) -> None:
        engine_ctx = _make_engine_ctx(tmp_path)
        runner = StepRunner(engine_ctx=engine_ctx)
        run_state = RunState(run_id="run-query", goal="test")
        step = Step(
            kind=StepKind.ANALYZE_REPO,
            title="Analyze",
            goal="analyze repository",
            inputs={"prompt": "inspect src"},
        )

        result = await runner.run_step(step, run_state)

        assert result.success is True
        assert result.summary == "handled:inspect src"
        assert result.query_state is not None
        assert result.query_state.messages[0].role == Role.SYSTEM

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from mini_cc.agent.bus import AgentEventBus, AgentLifecycleEvent
from mini_cc.context.engine_context import EngineContext
from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.harness import (
    BOOTSTRAP_FLOW_METADATA,
    CheckpointStore,
    RetryPolicy,
    RunBudget,
    RunHarness,
    RunState,
    RunStatus,
    Step,
    StepKind,
    StepStatus,
    prepare_run_request,
)
from mini_cc.models import Event, Message, Role, TextDelta, ToolCallDelta, ToolCallEnd, ToolCallStart
from mini_cc.query_engine.engine import QueryEngine
from mini_cc.tool_executor.executor import StreamingToolExecutor
from mini_cc.tools import create_default_registry


def _tool_count_since_last_user(messages: list[Message]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == Role.USER:
            return sum(1 for message in messages[index + 1 :] if message.role == Role.TOOL)
    return sum(1 for message in messages if message.role == Role.TOOL)


def _make_stream(repo_path: Path) -> Callable[[list[Message], list[dict[str, object]]], AsyncGenerator[Event, None]]:
    calc_path = repo_path / "calc.py"

    async def _stream(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
        del tools
        prompt = ""
        for message in reversed(messages):
            if message.role == Role.USER and message.content:
                prompt = message.content
                break

        if prompt == "Analyze the failing calc repository":
            yield TextDelta(content="Relevant files: calc.py and test_calc.py")
            return

        if prompt == "Fix calc.add so the local pytest command passes":
            tool_count = _tool_count_since_last_user(messages)
            if tool_count == 0:
                yield TextDelta(content="Inspecting calc.py")
                yield ToolCallStart(tool_call_id="tc_read", name="file_read")
                yield ToolCallDelta(tool_call_id="tc_read", arguments_json_delta=json.dumps({"file_path": str(calc_path)}))
                yield ToolCallEnd(tool_call_id="tc_read")
                return
            if tool_count == 1:
                yield TextDelta(content="Applying the minimal fix")
                yield ToolCallStart(tool_call_id="tc_edit", name="file_edit")
                yield ToolCallDelta(
                    tool_call_id="tc_edit",
                    arguments_json_delta=json.dumps(
                        {
                            "file_path": str(calc_path),
                            "old_string": "return a - b\n",
                            "new_string": "return a + b\n",
                        }
                    ),
                )
                yield ToolCallEnd(tool_call_id="tc_edit")
                return
            yield TextDelta(content="The bug in calc.add is fixed.")
            return

        if prompt == "Summarize the completed local repository repair":
            yield TextDelta(content="calc.add now returns a + b and the local pytest command passed.")
            return

        if prompt == "Summarize the completed local repository repair with task audit":
            yield TextDelta(content="calc.add was fixed, pytest passed, and the task audit artifact was generated.")
            return

        yield TextDelta(content=f"Unhandled prompt: {prompt}")

    return _stream


def _make_bootstrap_stream(repo_path: Path) -> Callable[[list[Message], list[dict[str, object]]], AsyncGenerator[Event, None]]:
    pyproject_path = repo_path / "pyproject.toml"
    calc_path = repo_path / "calc.py"
    tests_path = repo_path / "tests" / "test_calc.py"

    async def _stream(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
        del tools
        prompt = ""
        for message in reversed(messages):
            if message.role == Role.USER and message.content:
                prompt = message.content
                break

        if prompt.startswith("当前工作目录几乎为空。请先搭建一个最小可运行的项目骨架"):
            tool_count = _tool_count_since_last_user(messages)
            if tool_count == 0:
                yield TextDelta(content="Creating pyproject.toml")
                yield ToolCallStart(tool_call_id="tc_bootstrap_pyproject", name="file_write")
                yield ToolCallDelta(
                    tool_call_id="tc_bootstrap_pyproject",
                    arguments_json_delta=json.dumps(
                        {
                            "file_path": str(pyproject_path),
                            "content": (
                                "[project]\n"
                                "name = \"bootstrap-demo\"\n"
                                "version = \"0.1.0\"\n"
                                "requires-python = \">=3.11\"\n"
                            ),
                        }
                    ),
                )
                yield ToolCallEnd(tool_call_id="tc_bootstrap_pyproject")
                return
            if tool_count == 1:
                yield TextDelta(content="Creating calc.py")
                yield ToolCallStart(tool_call_id="tc_bootstrap_calc", name="file_write")
                yield ToolCallDelta(
                    tool_call_id="tc_bootstrap_calc",
                    arguments_json_delta=json.dumps(
                        {
                            "file_path": str(calc_path),
                            "content": (
                                "from __future__ import annotations\n\n\n"
                                "def add(a: int, b: int) -> int:\n"
                                "    return a + b\n"
                            ),
                        }
                    ),
                )
                yield ToolCallEnd(tool_call_id="tc_bootstrap_calc")
                return
            if tool_count == 2:
                yield TextDelta(content="Creating tests/test_calc.py")
                yield ToolCallStart(tool_call_id="tc_bootstrap_tests", name="file_write")
                yield ToolCallDelta(
                    tool_call_id="tc_bootstrap_tests",
                    arguments_json_delta=json.dumps(
                        {
                            "file_path": str(tests_path),
                            "content": (
                                "from __future__ import annotations\n\n"
                                "from calc import add\n\n\n"
                                "def test_add() -> None:\n"
                                "    assert add(2, 3) == 5\n"
                            ),
                        }
                    ),
                )
                yield ToolCallEnd(tool_call_id="tc_bootstrap_tests")
                return
            yield TextDelta(content="Bootstrap complete.")
            return

        if prompt == "从空仓库实现一个可测试的小项目":
            yield TextDelta(content="The bootstrap scaffold already satisfies the requested feature.")
            return

        if "分析当前仓库" in prompt:
            yield TextDelta(content="The repository now contains pyproject.toml, calc.py, and tests/test_calc.py.")
            return

        if "总结已完成工作" in prompt:
            yield TextDelta(content="The empty repository was bootstrapped into a runnable Python project and tests pass.")
            return

        yield TextDelta(content=f"Unhandled prompt: {prompt}")

    return _stream


def _make_engine_ctx(
    repo_path: Path,
    *,
    stream_fn: Callable[[list[Message], list[dict[str, object]]], AsyncGenerator[Event, None]] | None = None,
    lifecycle_bus: AgentEventBus | None = None,
) -> EngineContext:
    registry = create_default_registry()
    executor = StreamingToolExecutor(registry)
    engine = QueryEngine(
        stream_fn=stream_fn or _make_stream(repo_path),
        tool_use_ctx=ToolUseContext(
            get_schemas=registry.to_api_format,
            execute=executor.run,
        ),
        model="test-model",
    )
    return EngineContext(
        engine=engine,
        prompt_builder=SystemPromptBuilder(),
        env_info=collect_env_info("test-model", cwd=repo_path),
        lifecycle_bus=lifecycle_bus,
        model="test-model",
    )


def _write_local_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "calc.py").write_text(
        "from __future__ import annotations\n\n\ndef add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    (repo_path / "test_calc.py").write_text(
        (
            "from __future__ import annotations\n\n"
            "from calc import add\n\n\n"
            "def test_add() -> None:\n"
            "    assert add(2, 3) == 5\n"
        ),
        encoding="utf-8",
    )


def _write_retrying_verifier(repo_path: Path) -> Path:
    verifier_path = repo_path / "verify_once.py"
    verifier_path.write_text(
        (
            "from __future__ import annotations\n\n"
            "from pathlib import Path\n"
            "import sys\n\n"
            "state_path = Path('.verify_state')\n"
            "attempt = int(state_path.read_text(encoding='utf-8')) if state_path.exists() else 0\n"
            "attempt += 1\n"
            "state_path.write_text(str(attempt), encoding='utf-8')\n"
            "if attempt == 1:\n"
            "    print('verification failed on first attempt')\n"
            "    raise SystemExit(1)\n"
            "print('verification diagnostics succeeded on retry')\n"
        ),
        encoding="utf-8",
    )
    return verifier_path


class TestHarnessLocalRepoE2E:
    async def test_bootstraps_empty_repo_before_running_standard_flow(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir(parents=True, exist_ok=True)
        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(engine_ctx=_make_engine_ctx(repo_path, stream_fn=_make_bootstrap_stream(repo_path)), store=store)
        steps, metadata = prepare_run_request("从空仓库实现一个可测试的小项目", "build", repo_path)
        metadata["test_command"] = f"cd {repo_path} && python -m pytest -q"

        result = await harness.run(
            "从空仓库实现一个可测试的小项目",
            steps=steps,
            metadata=metadata,
        )

        restored = store.load_state(result.run_id)
        bootstrap_step = next(step for step in restored.steps if step.kind == StepKind.BOOTSTRAP_PROJECT)
        run_tests_step = next(step for step in restored.steps if step.kind == StepKind.RUN_TESTS)

        assert restored.status == RunStatus.COMPLETED
        assert restored.metadata[BOOTSTRAP_FLOW_METADATA] == "true"
        assert bootstrap_step.status == StepStatus.SUCCEEDED
        assert (repo_path / "pyproject.toml").is_file()
        assert (repo_path / "calc.py").is_file()
        assert (repo_path / "tests" / "test_calc.py").is_file()
        assert run_tests_step.status == StepStatus.SUCCEEDED
        assert "1 passed" in run_tests_step.summary

    async def test_repairs_local_repo_and_runs_real_pytest(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        _write_local_repo(repo_path)
        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(engine_ctx=_make_engine_ctx(repo_path), store=store)

        result = await harness.run(
            "Fix calc.add and verify locally",
            steps=[
                Step(
                    kind=StepKind.ANALYZE_REPO,
                    title="Analyze",
                    goal="Analyze the failing calc repository",
                    inputs={"prompt": "Analyze the failing calc repository"},
                ),
                Step(
                    kind=StepKind.EDIT_CODE,
                    title="Execute",
                    goal="Fix calc.add so the local pytest command passes",
                    inputs={"prompt": "Fix calc.add so the local pytest command passes"},
                ),
                Step(
                    kind=StepKind.FINALIZE,
                    title="Finalize",
                    goal="Summarize the completed local repository repair",
                    inputs={"prompt": "Summarize the completed local repository repair"},
                ),
            ],
            metadata={
                "test_command": f"cd {repo_path} && python -m pytest test_calc.py -q",
            },
        )

        restored = store.load_state(result.run_id)
        documentation = store.documentation_path(result.run_id).read_text(encoding="utf-8")
        events = store.load_events(result.run_id)
        run_tests_step = next(step for step in restored.steps if step.kind == StepKind.RUN_TESTS)
        run_tests_artifact = next(iter(run_tests_step.artifacts.values()))

        assert restored.status.value == "completed"
        assert "return a + b" in (repo_path / "calc.py").read_text(encoding="utf-8")
        assert run_tests_step.status.value == "succeeded"
        assert "1 passed" in run_tests_step.summary
        assert "1 passed" in Path(run_tests_artifact).read_text(encoding="utf-8")
        assert "Fix calc.add and verify locally" in documentation
        assert events
        assert any(event.event_type == "step_completed" for event in events)

    async def test_generates_task_audit_artifact_via_plugin_system(self, tmp_path: Path, monkeypatch) -> None:
        repo_path = tmp_path / "repo"
        _write_local_repo(repo_path)
        plugin_dir = tmp_path / ".mini_cc" / "task_audit_plugins"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "demo_task.py").write_text(
            (
                "from __future__ import annotations\n"
                "import json\n"
                "from pathlib import Path\n"
                "from mini_cc.harness.task_audit import TaskAuditProfile, TaskAuditResult\n\n"
                "class DemoTaskProfile(TaskAuditProfile):\n"
                "    profile_id = 'demo_task'\n"
                "    display_name = 'Demo Task'\n"
                "    artifact_name = 'demo_audit.json'\n\n"
                "    def parse_result(self, artifact_path: str) -> TaskAuditResult | None:\n"
                "        path = Path(artifact_path)\n"
                "        if path.name != self.artifact_name:\n"
                "            return None\n"
                "        try:\n"
                "            loaded = json.loads(path.read_text(encoding='utf-8'))\n"
                "        except json.JSONDecodeError:\n"
                "            return None\n"
                "        summary = loaded.get('summary', 'demo audit complete')\n"
                "        blockers = [str(item) for item in loaded.get('blockers', [])]\n"
                "        return TaskAuditResult(profile_id=self.profile_id, summary=str(summary), blockers=blockers, raw_artifact_path=str(path))\n\n"
                "def register() -> TaskAuditProfile:\n"
                "    return DemoTaskProfile()\n"
            ),
            encoding="utf-8",
        )
        audit_script = tmp_path / "demo_task_audit.py"
        audit_script.write_text(
            (
                "from __future__ import annotations\n"
                "import json\n"
                "import sys\n"
                "json.dump({'summary': 'demo audit passed', 'blockers': []}, sys.stdout)\n"
            ),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(engine_ctx=_make_engine_ctx(repo_path), store=store)

        result = await harness.run(
            "Fix calc.add and verify with audit",
            steps=[
                Step(
                    kind=StepKind.EDIT_CODE,
                    title="Execute",
                    goal="Fix calc.add so the local pytest command passes",
                    inputs={"prompt": "Fix calc.add so the local pytest command passes"},
                ),
                Step(
                    kind=StepKind.FINALIZE,
                    title="Finalize",
                    goal="Summarize the completed local repository repair with task audit",
                    inputs={"prompt": "Summarize the completed local repository repair with task audit"},
                ),
            ],
            metadata={
                "test_command": f"cd {repo_path} && python -m pytest test_calc.py -q",
                "audit_profile": "demo_task",
                "task_audit_command": f"python {audit_script}",
            },
        )

        restored = store.load_state(result.run_id)
        audit_step = next(step for step in restored.steps if step.kind == StepKind.RUN_TASK_AUDIT)
        documentation = store.documentation_path(result.run_id).read_text(encoding="utf-8")

        assert restored.status.value == "completed"
        assert "task_audit" in audit_step.artifacts
        assert Path(audit_step.artifacts["task_audit"]).is_file()
        assert "demo audit passed" in Path(audit_step.artifacts["task_audit"]).read_text(encoding="utf-8")
        assert "## 任务专项审计" in documentation
        assert "demo audit passed" in documentation

    async def test_failed_verification_generates_inspect_failures_step(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        _write_local_repo(repo_path)
        verifier_path = _write_retrying_verifier(repo_path)
        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(engine_ctx=_make_engine_ctx(repo_path), store=store)

        result = await harness.run(
            "Fix calc.add and inspect failed verification",
            steps=[
                Step(
                    kind=StepKind.EDIT_CODE,
                    title="Execute",
                    goal="Fix calc.add so the local verification command can eventually pass",
                    inputs={"prompt": "Fix calc.add so the local pytest command passes"},
                ),
                Step(
                    kind=StepKind.FINALIZE,
                    title="Finalize",
                    goal="Summarize the completed local repository repair",
                    inputs={"prompt": "Summarize the completed local repository repair"},
                ),
            ],
            retry_policy=RetryPolicy(max_step_retries=0),
            metadata={
                "test_command": f"cd {repo_path} && python {verifier_path}",
            },
        )

        restored = store.load_state(result.run_id)
        events = store.load_events(result.run_id)
        run_tests_step = next(step for step in restored.steps if step.kind == StepKind.RUN_TESTS)
        inspect_step = next(step for step in restored.steps if step.kind == StepKind.INSPECT_FAILURES)
        run_tests_event = next(
            event for event in events if event.event_type == "step_completed" and event.step_id == run_tests_step.id
        )

        assert restored.status == RunStatus.COMPLETED
        assert run_tests_step.status == StepStatus.FAILED_RETRYABLE
        assert "verification failed on first attempt" in run_tests_step.summary
        assert inspect_step.status == StepStatus.SUCCEEDED
        assert "verification diagnostics succeeded on retry" in inspect_step.summary
        assert "inspect_failures" in run_tests_event.data["inserted_steps"]
        assert restored.replan_count == 1

    async def test_cancelled_run_marks_inflight_step_terminal(self, tmp_path: Path) -> None:
        interrupt_seen = {"value": False}
        engine_ctx = _make_engine_ctx(tmp_path)

        async def _slow_stream(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
            del messages, tools
            while not engine_ctx.is_interrupted:
                await asyncio.sleep(0.02)
            interrupt_seen["value"] = True
            return
            yield

        engine_ctx.engine = QueryEngine(
            stream_fn=_slow_stream,
            tool_use_ctx=ToolUseContext(
                get_schemas=lambda: [],
                execute=lambda tool_calls, config=None: _empty_tool_results(),
                is_interrupted=lambda: engine_ctx.is_interrupted,
            ),
            model="test-model",
        )
        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(engine_ctx=engine_ctx, store=store)

        run_task = asyncio.create_task(
            harness.run(
                "Cancel a long-running plan step",
                steps=[Step(kind=StepKind.MAKE_PLAN, title="Plan", goal="Wait for cancellation")],
            )
        )
        await asyncio.sleep(0.1)
        run_id = next(iter(harness._run_interrupts))
        cancelled = harness.cancel(run_id)
        result = await asyncio.wait_for(run_task, timeout=2.0)

        assert cancelled.status == RunStatus.CANCELLED
        assert interrupt_seen["value"] is True
        assert result.status == RunStatus.CANCELLED
        assert result.steps[0].status == StepStatus.FAILED_TERMINAL

    async def test_timed_out_verification_generates_timeout_replan(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        _write_local_repo(repo_path)
        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(engine_ctx=_make_engine_ctx(repo_path), store=store)

        result = await harness.run(
            "Fix calc.add and handle a timed out verification step",
            steps=[
                Step(
                    kind=StepKind.EDIT_CODE,
                    title="Execute",
                    goal="Fix calc.add before running a deliberately slow verification command",
                    inputs={"prompt": "Fix calc.add so the local pytest command passes"},
                ),
            ],
            budget=RunBudget(max_step_seconds=1),
            retry_policy=RetryPolicy(max_step_retries=0),
            metadata={
                "test_command": f"cd {repo_path} && python -c \"import time; time.sleep(2)\"",
            },
        )

        restored = store.load_state(result.run_id)
        events = store.load_events(result.run_id)
        run_tests_step = next(step for step in restored.steps if step.kind == StepKind.RUN_TESTS)
        timeout_replans = [step for step in restored.steps if step.title == "Timeout Replan"]
        run_tests_event = next(
            event for event in events if event.event_type == "step_completed" and event.step_id == run_tests_step.id
        )

        assert restored.status == RunStatus.COMPLETED
        assert run_tests_step.status == StepStatus.FAILED_RETRYABLE
        assert run_tests_step.error == "Step timed out after 1s"
        assert timeout_replans
        assert any(step.kind == StepKind.MAKE_PLAN and step.status == StepStatus.SUCCEEDED for step in timeout_replans)
        assert any("timed out step was `run_tests`" in step.goal for step in timeout_replans)
        assert run_tests_event.data["decision"] == "replan"

    async def test_agent_lifecycle_events_flow_into_run_artifacts(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir(parents=True, exist_ok=True)
        lifecycle_bus = AgentEventBus()
        published = {"value": False}

        async def _agent_stream(messages: list[Message], tools: list[dict[str, object]]) -> AsyncGenerator[Event, None]:
            del tools
            prompt = ""
            for message in reversed(messages):
                if message.role == Role.USER and message.content:
                    prompt = message.content
                    break
            if prompt == "Inspect delegated findings":
                if not published["value"]:
                    lifecycle_bus.publish_nowait(
                        AgentLifecycleEvent(
                            event_type="created",
                            agent_id="agent-stale-1",
                            source_step_id="step-0001",
                            readonly=True,
                            scope_paths=[str(repo_path)],
                        )
                    )
                    lifecycle_bus.publish_nowait(
                        AgentLifecycleEvent(
                            event_type="completed",
                            agent_id="agent-stale-1",
                            success=False,
                            output_preview="stale delegated finding from an outdated workspace",
                            is_stale=True,
                            base_version_stamp="v1",
                            completed_version_stamp="v2",
                            termination_reason="failed to reconcile latest edits",
                        )
                    )
                    published["value"] = True
                yield TextDelta(content="Delegated investigation returned stale findings.")
                return
            if prompt == "Finalize delegated finding summary":
                yield TextDelta(content="The delegated finding was stale and should not be trusted.")
                return
            yield TextDelta(content=f"Unhandled prompt: {prompt}")

        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(
            engine_ctx=_make_engine_ctx(repo_path, stream_fn=_agent_stream, lifecycle_bus=lifecycle_bus),
            store=store,
        )

        result = await harness.run(
            "Track stale delegated findings",
            steps=[
                Step(
                    kind=StepKind.ANALYZE_REPO,
                    title="Analyze",
                    goal="Inspect delegated findings",
                    inputs={"prompt": "Inspect delegated findings"},
                ),
                Step(
                    kind=StepKind.FINALIZE,
                    title="Finalize",
                    goal="Finalize delegated finding summary",
                    inputs={"prompt": "Finalize delegated finding summary"},
                ),
            ],
        )

        restored = store.load_state(result.run_id)
        documentation = store.documentation_path(result.run_id).read_text(encoding="utf-8")

        assert restored.status == RunStatus.COMPLETED
        assert len(restored.spawned_agents) == 1
        assert restored.spawned_agents[0].agent_id == "agent-stale-1"
        assert restored.spawned_agents[0].is_stale is True
        assert restored.spawned_agents[0].termination_reason == "failed to reconcile latest edits"
        assert restored.metadata["agents_stale"] == "1"
        assert "stale results" in restored.metadata["agent_latest_issue"]
        assert "agent-stale-1" in documentation
        assert "stale delegated finding" in documentation


async def _empty_tool_results(tool_calls, config=None) -> AsyncGenerator[Event, None]:
    del tool_calls, config
    if False:
        yield TextDelta(content="")

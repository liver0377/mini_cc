from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from mini_cc.context.engine_context import EngineContext
from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.harness import CheckpointStore, RunBudget, RunHarness, RunStatus, Step, StepKind, StepStatus
from mini_cc.models import ToolCall, ToolResultEvent
from mini_cc.providers.openai import OpenAIProvider
from mini_cc.query_engine.engine import QueryEngine

_REAL_PROVIDER_ENV = "MINI_CC_RUN_REAL_PROVIDER_SMOKE"
_HAS_REAL_PROVIDER_CONFIG = bool(os.environ.get(_REAL_PROVIDER_ENV) == "1" and os.environ.get("OPENAI_API_KEY"))

pytestmark = pytest.mark.skipif(
    not _HAS_REAL_PROVIDER_CONFIG,
    reason=f"set {_REAL_PROVIDER_ENV}=1 and OPENAI_API_KEY to run the real provider smoke test",
)


async def _noop_execute(tool_calls: list[ToolCall]) -> AsyncGenerator[ToolResultEvent, None]:
    del tool_calls
    if False:
        yield ToolResultEvent(tool_call_id="", name="", output="", success=False)


def _make_real_provider_engine_ctx(repo_path: Path) -> EngineContext:
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    base_url = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    api_key = os.environ["OPENAI_API_KEY"]
    provider = OpenAIProvider(model=model, base_url=base_url, api_key=api_key)
    engine = QueryEngine(
        stream_fn=provider.stream,
        tool_use_ctx=ToolUseContext(get_schemas=lambda: [], execute=_noop_execute),
        model=model,
    )
    return EngineContext(
        engine=engine,
        prompt_builder=SystemPromptBuilder(),
        env_info=collect_env_info(model, cwd=repo_path),
        model=model,
    )


class TestRealProviderSmoke:
    async def test_completes_minimal_harness_run(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir(parents=True, exist_ok=True)
        store = CheckpointStore(base_dir=tmp_path / "runs")
        harness = RunHarness.create_default(engine_ctx=_make_real_provider_engine_ctx(repo_path), store=store)

        result = await harness.run(
            "Real provider smoke test",
            steps=[
                Step(
                    kind=StepKind.MAKE_PLAN,
                    title="Smoke",
                    goal="Return one short sentence acknowledging this smoke test without using tools.",
                    inputs={
                        "prompt": (
                            "Reply with one short plain-text sentence acknowledging this smoke test. "
                            "Do not call any tools. Do not use markdown."
                        )
                    },
                )
            ],
            budget=RunBudget(max_runtime_seconds=120, max_step_seconds=60),
        )

        restored = store.load_state(result.run_id)
        documentation_path = store.documentation_path(result.run_id)

        assert restored.status == RunStatus.COMPLETED
        assert restored.steps[0].status == StepStatus.SUCCEEDED
        assert restored.steps[0].summary.strip()
        assert documentation_path.is_file()
        assert "Real provider smoke test" in documentation_path.read_text(encoding="utf-8")

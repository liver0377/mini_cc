from __future__ import annotations

from pathlib import Path

from mini_cc.harness.bootstrap import (
    BOOTSTRAP_FLOW_METADATA,
    BOOTSTRAP_STEP_BUDGET_SECONDS,
    EDIT_CODE_STEP_BUDGET_SECONDS,
    is_bootstrap_candidate,
    prepare_run_request,
)
from mini_cc.harness.models import StepKind
from mini_cc.harness.audit import TaskAuditProfile, TaskAuditRegistry


class TestBootstrapDetection:
    def test_empty_repo_with_only_git_metadata_is_bootstrap_candidate(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".mini_cc").mkdir()
        (tmp_path / "README.md").write_text("# notes\n", encoding="utf-8")
        (tmp_path / ".gitignore").write_text(".venv\n", encoding="utf-8")

        assert is_bootstrap_candidate(tmp_path) is True

    def test_repo_with_real_source_file_is_not_bootstrap_candidate(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")

        assert is_bootstrap_candidate(tmp_path) is False


class _DummyProfile(TaskAuditProfile):
    profile_id = "dummy"
    display_name = "Dummy"
    keywords = ["dummy-task", "dummy task"]
    description = "A dummy task for testing."
    default_test_command = "python -m pytest"
    bootstrap_guidance = "Please create a dummy entry point."
    audit_command = "python audit_dummy.py"

    def parse_result(self, artifact_path: str) -> None:
        return None


def _register_dummy(module: object) -> None:
    pass


def _make_registry_with_dummy() -> TaskAuditRegistry:
    registry = TaskAuditRegistry()
    registry._profiles["dummy"] = _DummyProfile()
    return registry


class TestPrepareRunRequest:
    def test_build_mode_adds_bootstrap_step_for_empty_repo(self, tmp_path: Path) -> None:
        steps, metadata = prepare_run_request("实现一个 mini-jq 子集", "build", tmp_path)

        assert steps[0].kind == StepKind.BOOTSTRAP_PROJECT
        assert steps[1].kind == StepKind.ANALYZE_REPO
        assert steps[2].kind == StepKind.EDIT_CODE
        assert steps[3].kind == StepKind.FINALIZE
        assert steps[0].budget_seconds == BOOTSTRAP_STEP_BUDGET_SECONDS
        assert steps[2].budget_seconds == EDIT_CODE_STEP_BUDGET_SECONDS
        assert metadata[BOOTSTRAP_FLOW_METADATA] == "true"
        assert metadata["test_command"] == "uv run pytest -q"
        assert metadata["audit_profile"] == "mini_jq"
        assert "scripts/task_audit/mini_jq.py" in metadata["task_audit_command"]
        prompt_text = str(steps[0].inputs["prompt"])
        assert "可执行入口 `mini-jq`" in prompt_text
        assert "审计任务" in prompt_text

    def test_build_mode_keeps_standard_flow_for_non_empty_repo(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")

        steps, metadata = prepare_run_request("修复测试", "build", tmp_path)

        assert [step.kind for step in steps] == [
            StepKind.ANALYZE_REPO,
            StepKind.EDIT_CODE,
            StepKind.FINALIZE,
        ]
        assert steps[1].budget_seconds == EDIT_CODE_STEP_BUDGET_SECONDS
        assert metadata == {}

    def test_non_empty_repo_still_auto_binds_mini_jq_audit(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='jq-demo'\nversion='0.1.0'\n", encoding="utf-8")

        steps, metadata = prepare_run_request("请实现一个 mini jq，支持 pipe 语法", "build", tmp_path)

        assert [step.kind for step in steps] == [
            StepKind.ANALYZE_REPO,
            StepKind.EDIT_CODE,
            StepKind.FINALIZE,
        ]
        assert metadata["audit_profile"] == "mini_jq"
        assert "scripts/task_audit/mini_jq.py" in metadata["task_audit_command"]
        assert "test_command" not in metadata

    def test_plan_mode_never_bootstraps(self, tmp_path: Path) -> None:
        steps, metadata = prepare_run_request("先做计划", "plan", tmp_path)

        assert [step.kind for step in steps] == [StepKind.MAKE_PLAN, StepKind.FINALIZE]
        assert metadata == {}

    def test_custom_registry_profile_matched_by_keywords(self, tmp_path: Path) -> None:
        registry = _make_registry_with_dummy()
        steps, metadata = prepare_run_request("请实现一个 dummy-task 功能", "build", tmp_path, registry=registry)

        assert metadata["audit_profile"] == "dummy"
        assert metadata["task_audit_command"] == "python audit_dummy.py"
        assert metadata["test_command"] == "python -m pytest"
        prompt_text = str(steps[0].inputs["prompt"])
        assert "dummy" in prompt_text

    def test_scaffold_files_copied_on_bootstrap(self, tmp_path: Path) -> None:
        registry = TaskAuditRegistry()
        steps, metadata = prepare_run_request("实现一个 mini-jq 子集", "build", tmp_path, registry=registry)

        assert (tmp_path / "pyproject.toml").is_file()
        assert (tmp_path / "src" / "mini_jq" / "cli.py").is_file()
        assert (tmp_path / "tests" / "test_mini_jq.py").is_file()
        assert (tmp_path / "scripts" / "task_audit" / "mini_jq.py").is_file()
        assert (tmp_path / "scripts" / "task_audit" / "cases" / "mini_jq_cases.json").is_file()

    def test_no_scaffold_when_profile_has_no_scaffold_dir(self, tmp_path: Path) -> None:
        registry = _make_registry_with_dummy()
        prepare_run_request("实现一个 dummy-task 功能", "build", tmp_path, registry=registry)

        assert not (tmp_path / "pyproject.toml").exists()

    def test_no_scaffold_for_non_empty_repo(self, tmp_path: Path) -> None:
        (tmp_path / "existing.py").write_text("x = 1\n", encoding="utf-8")
        registry = TaskAuditRegistry()
        prepare_run_request("实现一个 mini-jq 子集", "build", tmp_path, registry=registry)

        assert not (tmp_path / "src").exists()

    def test_audit_context_injected_into_bootstrap_prompt(self, tmp_path: Path) -> None:
        registry = TaskAuditRegistry()
        steps, _ = prepare_run_request("实现一个 mini-jq 子集", "build", tmp_path, registry=registry)

        prompt_text = str(steps[0].inputs["prompt"])
        assert "审计任务" in prompt_text
        assert "mini_jq" in prompt_text

    def test_goal_fields_do_not_contain_user_text_with_brackets(self, tmp_path: Path) -> None:
        steps, _ = prepare_run_request("实现 .items[0] 和 .foo[1] 功能", "build", tmp_path)

        for step in steps:
            assert "[" not in step.goal
            assert "]" not in step.goal


class TestMatchScore:
    def test_exact_keyword_match(self) -> None:
        profile = _DummyProfile()
        assert profile.match_score("实现一个 dummy-task") > 0.0

    def test_no_match(self) -> None:
        profile = _DummyProfile()
        assert profile.match_score("实现一个 JSON 解析器") == 0.0

    def test_partial_keyword_no_match(self) -> None:
        profile = _DummyProfile()
        score = profile.match_score("dummy")
        assert score == 0.0

    def test_profile_with_no_keywords(self) -> None:
        profile = TaskAuditProfile()
        assert profile.match_score("anything") == 0.0

from __future__ import annotations

from pathlib import Path

from mini_cc.harness.bootstrap import BOOTSTRAP_FLOW_METADATA, is_bootstrap_candidate, prepare_run_request
from mini_cc.harness.models import StepKind


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


class TestPrepareRunRequest:
    def test_build_mode_adds_bootstrap_step_for_empty_repo(self, tmp_path: Path) -> None:
        steps, metadata = prepare_run_request("实现一个 mini-jq 子集", "build", tmp_path)

        assert steps[0].kind == StepKind.BOOTSTRAP_PROJECT
        assert steps[1].kind == StepKind.ANALYZE_REPO
        assert steps[2].kind == StepKind.EDIT_CODE
        assert steps[3].kind == StepKind.FINALIZE
        assert metadata[BOOTSTRAP_FLOW_METADATA] == "true"
        assert metadata["test_command"] == "uv run pytest -q"
        assert metadata["audit_profile"] == "mini_jq"
        assert "scripts/task_audit/mini_jq.py" in metadata["task_audit_command"]
        assert "可执行入口 `mini-jq`" in str(steps[0].inputs["prompt"])

    def test_build_mode_keeps_standard_flow_for_non_empty_repo(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")

        steps, metadata = prepare_run_request("修复测试", "build", tmp_path)

        assert [step.kind for step in steps] == [
            StepKind.ANALYZE_REPO,
            StepKind.EDIT_CODE,
            StepKind.FINALIZE,
        ]
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

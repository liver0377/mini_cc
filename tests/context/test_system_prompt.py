from __future__ import annotations

from pathlib import Path

from mini_cc.context.system_prompt import (
    EnvInfo,
    SystemPromptBuilder,
    _build_harness_context,
    _display_model_name,
    _format_env_section,
    _load_static_prompts,
    _read_agents_md,
    collect_env_info,
)
from mini_cc.harness import CheckpointStore, RunState, RunStatus
from mini_cc.harness.iteration import IterationOutcome, IterationReview, IterationScore


class TestDisplayModelName:
    def test_known_model(self) -> None:
        assert _display_model_name("gpt-4o") == "GPT-4o"

    def test_unknown_model(self) -> None:
        assert _display_model_name("my-custom-model") == "my-custom-model"


class TestLoadStaticPrompts:
    def test_returns_nonempty_list(self) -> None:
        parts = _load_static_prompts()
        assert len(parts) == 4
        assert all(isinstance(p, str) for p in parts)
        assert all(len(p) > 0 for p in parts)


class TestReadAgentsMd:
    def test_file_exists(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# Project guide\nDo things.", encoding="utf-8")
        result = _read_agents_md(str(tmp_path))
        assert result is not None
        assert "Project guide" in result

    def test_file_missing(self, tmp_path: Path) -> None:
        result = _read_agents_md(str(tmp_path))
        assert result is None


class TestFormatEnvSection:
    def test_build_mode(self) -> None:
        env = EnvInfo(
            working_directory="/home/user/project",
            is_git_repo=True,
            platform_name="linux",
            shell="/bin/bash",
            os_version="Linux 6.8.0",
            model_name="GPT-4o",
            model_id="gpt-4o",
        )
        result = _format_env_section(env, "build")
        assert "Working directory: /home/user/project" in result
        assert "Is directory a git repo: Yes" in result
        assert "Platform: linux" in result
        assert "Mode: build" in result
        assert "GPT-4o" in result
        assert "gpt-4o" in result

    def test_plan_mode(self) -> None:
        env = EnvInfo(
            working_directory="/tmp",
            is_git_repo=False,
            platform_name="darwin",
            shell="/bin/zsh",
            os_version="Darwin 25.3.0",
            model_name="Claude Sonnet 4",
            model_id="claude-sonnet-4-20250514",
        )
        result = _format_env_section(env, "plan")
        assert "Mode: plan" in result
        assert "Is directory a git repo: No" in result


class TestSystemPromptBuilder:
    def test_build_without_agents_md(self, tmp_path: Path) -> None:
        builder = SystemPromptBuilder()
        env = EnvInfo(
            working_directory=str(tmp_path),
            is_git_repo=False,
            platform_name="linux",
            shell="/bin/bash",
            os_version="Linux 6.8.0",
            model_name="GPT-4o",
            model_id="gpt-4o",
        )
        result = builder.build(env, mode="build")
        assert "Mode: build" in result
        assert "GPT-4o" in result
        assert "先读代码再改代码" in result or "先读后改" in result

    def test_build_with_agents_md(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# My Project\nUse pytest.", encoding="utf-8")

        builder = SystemPromptBuilder()
        env = EnvInfo(
            working_directory=str(tmp_path),
            is_git_repo=False,
            platform_name="linux",
            shell="/bin/bash",
            os_version="Linux 6.8.0",
            model_name="GPT-4o",
            model_id="gpt-4o",
        )
        result = builder.build(env, mode="plan")
        assert "My Project" in result
        assert "Use pytest." in result
        assert "Mode: plan" in result

    def test_build_plan_mode(self, tmp_path: Path) -> None:
        builder = SystemPromptBuilder()
        env = EnvInfo(
            working_directory=str(tmp_path),
            is_git_repo=False,
            platform_name="linux",
            shell="/bin/bash",
            os_version="Linux 6.8.0",
            model_name="GPT-4o",
            model_id="gpt-4o",
        )
        result = builder.build(env, mode="plan")
        assert "Mode: plan" in result

    def test_build_includes_harness_context(self, tmp_path: Path) -> None:
        store = CheckpointStore(base_dir=tmp_path / ".mini_cc" / "runs")
        run = RunState(run_id="run-ctx", goal="Fix tests")
        store.save_state(run)
        store.append_iteration_review(
            IterationReview(
                run_id="run-ctx",
                step_id="step-1",
                outcome=IterationOutcome.REGRESSED,
                score=IterationScore(total=-1, penalty=1),
                root_cause="pytest failed",
                next_constraints=["Keep using `uv run pytest`"],
                recommended_step_kind="inspect_failures",
            )
        )
        store.append_journal_entry("run-ctx", "## step-1 `run_tests`\n- Root cause: pytest failed\n")

        builder = SystemPromptBuilder()
        env = EnvInfo(
            working_directory=str(tmp_path),
            is_git_repo=False,
            platform_name="linux",
            shell="/bin/bash",
            os_version="Linux 6.8.0",
            model_name="GPT-4o",
            model_id="gpt-4o",
        )

        result = builder.build(env, mode="build", run_id="run-ctx")

        assert "<run_context>" in result
        assert "Run ID: run-ctx" in result
        assert "pytest failed" in result
        assert "inspect_failures" in result

    def test_build_prefers_documentation_lessons(self, tmp_path: Path) -> None:
        store = CheckpointStore(base_dir=tmp_path / ".mini_cc" / "runs")
        run = RunState(run_id="run-doc", goal="Fix tests")
        store.save_state(run)
        store.save_documentation(
            "run-doc",
            "\n".join(
                [
                    "# Run run-doc Documentation",
                    "",
                    "## 经验教训",
                    "",
                    "### 项目知识",
                    "",
                    "- 验证命令基线：`uv run pytest tests/`",
                    "",
                    "### 失败教训",
                    "",
                    "- 不要同时修改 3 个以上文件",
                    "",
                    "### 有效策略",
                    "",
                    "- 先分析再修改",
                    "",
                    "## 其他段落",
                    "",
                    "ignored",
                ]
            ),
        )
        store.append_journal_entry("run-doc", "this should not be used first\n")

        builder = SystemPromptBuilder()
        env = EnvInfo(
            working_directory=str(tmp_path),
            is_git_repo=False,
            platform_name="linux",
            shell="/bin/bash",
            os_version="Linux 6.8.0",
            model_name="GPT-4o",
            model_id="gpt-4o",
        )

        result = builder.build(env, mode="build", run_id="run-doc")

        assert "Lessons from current run:" in result
        assert "验证命令基线：`uv run pytest tests/`" in result
        assert "不要同时修改 3 个以上文件" in result
        assert "this should not be used first" not in result

    def test_static_parts_are_cached(self) -> None:
        builder = SystemPromptBuilder()
        parts1 = builder._static_parts
        parts2 = builder._static_parts
        assert parts1 is parts2


class TestBuildHarnessContext:
    def test_returns_none_without_runs(self, tmp_path: Path) -> None:
        assert _build_harness_context(str(tmp_path)) is None

    def test_falls_back_to_latest_terminal_run_documentation(self, tmp_path: Path) -> None:
        store = CheckpointStore(base_dir=tmp_path / ".mini_cc" / "runs")
        active = RunState(run_id="run-active", goal="active")
        completed = RunState(run_id="run-done", goal="done", status=RunStatus.COMPLETED, phase="completed")
        store.save_state(active)
        store.save_state(completed)
        store.save_documentation(
            "run-done",
            "\n".join(
                [
                    "# Run run-done Documentation",
                    "",
                    "## 经验教训",
                    "",
                    "### 项目知识",
                    "",
                    "- 使用 `uv run pytest`",
                ]
            ),
        )

        result = _build_harness_context(str(tmp_path), run_id="run-active")

        assert result is not None
        assert "Run ID: run-active" in result
        assert "Run goal: active" in result
        assert "Lessons from previous completed run (run-done):" in result
        assert "使用 `uv run pytest`" in result

    def test_current_run_context_keeps_reviews_and_appends_previous_lessons(self, tmp_path: Path) -> None:
        store = CheckpointStore(base_dir=tmp_path / ".mini_cc" / "runs")
        active = RunState(run_id="run-active", goal="active goal")
        completed = RunState(run_id="run-done", goal="done", status=RunStatus.COMPLETED, phase="completed")
        store.save_state(active)
        store.save_state(completed)
        store.append_iteration_review(
            IterationReview(
                run_id="run-active",
                step_id="step-1",
                outcome=IterationOutcome.REGRESSED,
                score=IterationScore(total=-1, penalty=1),
                root_cause="current run failed",
                next_constraints=["keep current context"],
                recommended_step_kind="make_plan",
            )
        )
        store.append_journal_entry("run-active", "## step-1 `run_tests`\n- Root cause: current run failed\n")
        store.save_documentation(
            "run-done",
            "\n".join(
                [
                    "# Run run-done Documentation",
                    "",
                    "## 经验教训",
                    "",
                    "### 项目知识",
                    "",
                    "- use previous lessons",
                ]
            ),
        )

        result = _build_harness_context(str(tmp_path), run_id="run-active")

        assert result is not None
        assert "Run ID: run-active" in result
        assert "current run failed" in result
        assert "Journal tail:" in result
        assert "Lessons from previous completed run (run-done):" in result
        assert "use previous lessons" in result


class TestCollectEnvInfo:
    def test_basic_fields(self) -> None:
        env = collect_env_info("gpt-4o", cwd=Path("/tmp"))
        assert env.model_id == "gpt-4o"
        assert env.model_name == "GPT-4o"
        assert env.platform_name in ("linux", "darwin")
        assert "/tmp" in env.working_directory

    def test_unknown_model(self) -> None:
        env = collect_env_info("some-random-model", cwd=Path("/tmp"))
        assert env.model_name == "some-random-model"

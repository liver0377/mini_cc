from __future__ import annotations

from pathlib import Path

from mini_cc.context.system_prompt import (
    EnvInfo,
    SystemPromptBuilder,
    _display_model_name,
    _format_env_section,
    _load_static_prompts,
    _read_agents_md,
    collect_env_info,
)


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

    def test_static_parts_are_cached(self) -> None:
        builder = SystemPromptBuilder()
        parts1 = builder._static_parts
        parts2 = builder._static_parts
        assert parts1 is parts2


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

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_STATIC_FILES = ("intro.md", "rules.md", "caution.md", "tool_guide.md")
_AGENTS_MD = "AGENTS.md"


@dataclass(frozen=True)
class EnvInfo:
    working_directory: str
    is_git_repo: bool
    platform_name: str
    shell: str
    os_version: str
    model_name: str
    model_id: str


def collect_env_info(model: str, cwd: Path | None = None) -> EnvInfo:
    work_dir = str(cwd or Path.cwd())
    is_git = _is_git_repo(work_dir)
    return EnvInfo(
        working_directory=work_dir,
        is_git_repo=is_git,
        platform_name=platform.system().lower(),
        shell=os.environ.get("SHELL", "unknown"),
        os_version=platform.platform(),
        model_name=_display_model_name(model),
        model_id=model,
    )


def _is_git_repo(path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and "true" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _display_model_name(model: str) -> str:
    known: dict[str, str] = {
        "gpt-4o": "GPT-4o",
        "gpt-4o-mini": "GPT-4o Mini",
        "gpt-4-turbo": "GPT-4 Turbo",
        "claude-sonnet-4-20250514": "Claude Sonnet 4",
        "claude-3-5-sonnet-20241022": "Claude 3.5 Sonnet",
        "deepseek-chat": "DeepSeek Chat",
        "deepseek-reasoner": "DeepSeek Reasoner",
    }
    return known.get(model, model)


def _load_static_prompts() -> list[str]:
    parts: list[str] = []
    for filename in _STATIC_FILES:
        path = _PROMPTS_DIR / filename
        text = path.read_text(encoding="utf-8").strip()
        if text:
            parts.append(text)
    return parts


def _read_agents_md(cwd: str) -> str | None:
    path = Path(cwd) / _AGENTS_MD
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return None


def _format_env_section(env: EnvInfo, mode: str) -> str:
    git_status = "Yes" if env.is_git_repo else "No"
    lines = [
        "<env>",
        f"Working directory: {env.working_directory}",
        f"Is directory a git repo: {git_status}",
        f"Platform: {env.platform_name}",
        f"Shell: {env.shell}",
        f"OS Version: {env.os_version}",
        f"Mode: {mode}",
        "</env>",
        f"You are powered by the model named {env.model_name}. The exact model ID is {env.model_id}.",
    ]
    return "\n".join(lines)


class SystemPromptBuilder:
    def __init__(self) -> None:
        self._static_parts: list[str] = _load_static_prompts()

    def build(self, env: EnvInfo, mode: str = "build") -> str:
        parts = list(self._static_parts)
        parts.append(_format_env_section(env, mode))

        agents_md = _read_agents_md(env.working_directory)
        if agents_md:
            parts.append(agents_md)

        return "\n\n".join(parts)

from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mini_cc.memory.store import load_memory_index

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_STATIC_FILES = ("intro.md", "rules.md", "caution.md", "tool_guide.md")
_SUB_STATIC_FILES = ("intro_sub.md", "rules_sub.md", "caution.md", "tool_guide_sub.md")
_AGENTS_MD = "AGENTS.md"
_MAX_REVIEW_COUNT = 3
_MAX_JOURNAL_LINES = 12
_MAX_LESSON_LINES = 12


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


def _load_static_prompts(files: tuple[str, ...] = _STATIC_FILES) -> list[str]:
    parts: list[str] = []
    for filename in files:
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


def _build_harness_context(cwd: str, run_id: str | None = None) -> str | None:
    runs_dir = Path(cwd) / ".mini_cc" / "runs"
    target_run_id = run_id or _latest_run_id(runs_dir)
    if target_run_id is None:
        return None

    if run_id is not None:
        context = _render_current_run_context(runs_dir, target_run_id)
        if context is None:
            return None
        fallback_run_id = _latest_terminal_run_id(runs_dir, exclude_run_id=target_run_id)
        if fallback_run_id is not None:
            fallback_lessons = _extract_lessons(runs_dir / fallback_run_id / "Documentation.md")
            if fallback_lessons:
                context = _append_lessons_block(
                    context,
                    header=f"Lessons from previous completed run ({fallback_run_id}):",
                    lessons=fallback_lessons,
                )
        return context

    if _extract_lessons(runs_dir / target_run_id / "Documentation.md"):
        return _render_run_context(runs_dir, target_run_id, lessons_only=True)
    return _render_run_context(runs_dir, target_run_id, lessons_only=False)


def _latest_run_id(runs_dir: Path) -> str | None:
    candidates: list[tuple[float, str]] = []
    for state_path in runs_dir.glob("*/state.json"):
        try:
            candidates.append((state_path.stat().st_mtime, state_path.parent.name))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _latest_terminal_run_id(runs_dir: Path, exclude_run_id: str | None = None) -> str | None:
    terminal_states = {"completed", "failed", "blocked", "cancelled", "timed_out"}
    candidates: list[tuple[float, str]] = []
    for state_path in runs_dir.glob("*/state.json"):
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(loaded, dict):
            continue
        run_id = state_path.parent.name
        if exclude_run_id is not None and run_id == exclude_run_id:
            continue
        if loaded.get("status") not in terminal_states:
            continue
        try:
            candidates.append((state_path.stat().st_mtime, run_id))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _render_run_context(runs_dir: Path, target_run_id: str, *, lessons_only: bool) -> str | None:
    run_dir = runs_dir / target_run_id
    state = _load_run_state_dict(run_dir / "state.json")
    if state is None:
        return None

    lines = [
        "<run_context>",
        f"Run ID: {state.get('run_id', target_run_id)}",
        f"Run status: {state.get('status', 'unknown')}",
        f"Run phase: {state.get('phase', 'unknown')}",
        f"Run goal: {state.get('goal', '')}",
    ]

    lessons = _extract_lessons(run_dir / "Documentation.md")
    if lessons:
        lines.append("Lessons from previous run:")
        lines.extend(f"- {line}" for line in lessons)
    elif not lessons_only:
        reviews = _load_review_dicts(run_dir / "iteration_reviews.jsonl")
        if reviews:
            lines.append("Latest iteration reviews:")
            for review in reviews[-_MAX_REVIEW_COUNT:]:
                constraints_list = review.get("next_constraints", [])
                constraints = "; ".join(constraints_list) if constraints_list else "none"
                recommendation = review.get("recommended_step_kind") or "none"
                lines.append(
                    f"- {review.get('step_id', 'unknown')}: {review.get('outcome', 'unknown')}; "
                    f"root_cause={review.get('root_cause', '')}; "
                    f"constraints={constraints}; next={recommendation}"
                )

        journal_path = run_dir / "journal.md"
        if journal_path.is_file():
            journal_lines = [
                line.strip()
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if journal_lines:
                lines.append("Journal tail:")
                lines.extend(f"- {line}" for line in journal_lines[-_MAX_JOURNAL_LINES:])

    lines.append("</run_context>")
    return "\n".join(lines)


def _render_current_run_context(runs_dir: Path, target_run_id: str) -> str | None:
    run_dir = runs_dir / target_run_id
    state = _load_run_state_dict(run_dir / "state.json")
    if state is None:
        return None

    lines = [
        "<run_context>",
        f"Run ID: {state.get('run_id', target_run_id)}",
        f"Run status: {state.get('status', 'unknown')}",
        f"Run phase: {state.get('phase', 'unknown')}",
        f"Run goal: {state.get('goal', '')}",
    ]

    lessons = _extract_lessons(run_dir / "Documentation.md")
    if lessons:
        lines.append("Lessons from current run:")
        lines.extend(f"- {line}" for line in lessons)
    else:
        reviews = _load_review_dicts(run_dir / "iteration_reviews.jsonl")
        if reviews:
            lines.append("Latest iteration reviews:")
            for review in reviews[-_MAX_REVIEW_COUNT:]:
                constraints_list = review.get("next_constraints", [])
                constraints = "; ".join(constraints_list) if constraints_list else "none"
                recommendation = review.get("recommended_step_kind") or "none"
                lines.append(
                    f"- {review.get('step_id', 'unknown')}: {review.get('outcome', 'unknown')}; "
                    f"root_cause={review.get('root_cause', '')}; "
                    f"constraints={constraints}; next={recommendation}"
                )

        journal_path = run_dir / "journal.md"
        if journal_path.is_file():
            journal_lines = [
                line.strip()
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if journal_lines:
                lines.append("Journal tail:")
                lines.extend(f"- {line}" for line in journal_lines[-_MAX_JOURNAL_LINES:])

    lines.append("</run_context>")
    return "\n".join(lines)


def _append_lessons_block(context: str, *, header: str, lessons: list[str]) -> str:
    closing_tag = "</run_context>"
    if not context.endswith(closing_tag):
        return context
    prefix = context[: -len(closing_tag)].rstrip()
    extra_lines = [header, *[f"- {line}" for line in lessons], closing_tag]
    return prefix + "\n" + "\n".join(extra_lines)


def _load_run_state_dict(path: Path) -> dict[str, object] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _extract_lessons(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    heading = "## 经验教训"
    start = content.find(heading)
    if start < 0:
        return []
    remainder = content[start + len(heading) :].strip()
    next_section = remainder.find("\n## ")
    if next_section >= 0:
        remainder = remainder[:next_section].strip()
    lesson_lines = [
        line.strip().removeprefix("-").strip()
        for line in remainder.splitlines()
        if line.strip() and not line.strip().startswith("###")
    ]
    return lesson_lines[:_MAX_LESSON_LINES]


def _load_review_dicts(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    reviews: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            reviews.append(loaded)
    return reviews


class SystemPromptBuilder:
    def __init__(self) -> None:
        self._static_parts: list[str] = _load_static_prompts()
        self._static_sub_parts: list[str] = _load_static_prompts(_SUB_STATIC_FILES)

    def build(
        self,
        env: EnvInfo,
        mode: str = "build",
        run_id: str | None = None,
        context_cwd: str | None = None,
    ) -> str:
        parts = list(self._static_parts)
        parts.append(_format_env_section(env, mode))

        agents_md = _read_agents_md(env.working_directory)
        if agents_md:
            parts.append(agents_md)

        memory_index = load_memory_index(Path(env.working_directory))
        if memory_index:
            parts.append(memory_index)

        harness_context = _build_harness_context(context_cwd or env.working_directory, run_id=run_id)
        if harness_context:
            parts.append(harness_context)

        return "\n\n".join(parts)

    def build_for_sub_agent(
        self,
        env: EnvInfo,
        mode: str = "build",
        run_id: str | None = None,
        context_cwd: str | None = None,
    ) -> str:
        parts = list(self._static_sub_parts)
        parts.append(_format_env_section(env, mode))

        agents_md = _read_agents_md(env.working_directory)
        if agents_md:
            parts.append(agents_md)

        memory_index = load_memory_index(Path(env.working_directory))
        if memory_index:
            parts.append(memory_index)

        harness_context = _build_harness_context(context_cwd or env.working_directory, run_id=run_id)
        if harness_context:
            parts.append(harness_context)

        return "\n\n".join(parts)

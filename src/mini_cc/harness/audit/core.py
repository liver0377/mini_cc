from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from mini_cc.harness.models import Step, StepKind


class TaskAuditResult(BaseModel):
    profile_id: str
    summary: str
    score_total: int | None = None
    dimensions: dict[str, str] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    recommended_next_focus: str | None = None
    raw_artifact_path: str | None = None


class TaskAuditProfile:
    profile_id: str = ""
    display_name: str = ""
    artifact_name: str = "audit.json"
    keywords: list[str] = []
    description: str = ""
    scaffold_dir: str | None = None
    default_test_command: str = "uv run pytest -q"
    bootstrap_guidance: str = ""
    audit_command: str | None = None

    def match_score(self, user_text: str) -> float:
        if not self.keywords:
            return 0.0
        normalized = user_text.lower().replace("_", "-")
        hits = 0
        for kw in self.keywords:
            if kw.lower() in normalized:
                hits += 1
        return hits / len(self.keywords) if self.keywords else 0.0

    def parse_result(self, artifact_path: str) -> TaskAuditResult | None:
        raise NotImplementedError

    def snapshot_metadata(self, result: TaskAuditResult) -> dict[str, str]:
        metadata = {
            "audit_profile": result.profile_id,
            "audit_summary": result.summary,
        }
        if result.score_total is not None:
            metadata["audit_score_total"] = str(result.score_total)
        if result.blockers:
            metadata["audit_blockers"] = " | ".join(result.blockers)
        if result.improvements:
            metadata["audit_improvements"] = " | ".join(result.improvements)
        if result.regressions:
            metadata["audit_regressions"] = " | ".join(result.regressions)
        if result.recommended_next_focus:
            metadata["audit_next_focus"] = result.recommended_next_focus
        for key, value in result.dimensions.items():
            metadata[f"audit_{key}"] = value
        if result.raw_artifact_path:
            metadata["audit_artifact_path"] = result.raw_artifact_path
        return metadata

    def review_metadata(
        self,
        current: TaskAuditResult,
        previous: TaskAuditResult | None,
    ) -> tuple[dict[str, str], str | None, list[str]]:
        metadata = self.snapshot_metadata(current)
        constraints: list[str] = []
        if previous is not None and previous.summary != current.summary:
            metadata["audit_progress"] = "changed"
        elif previous is None:
            metadata["audit_progress"] = "baseline"
        else:
            metadata["audit_progress"] = "unchanged"
        if current.blockers:
            metadata["audit_blocker"] = current.blockers[0]
            constraints.append(f"Address audit blocker first: {current.blockers[0]}")
        if current.recommended_next_focus:
            metadata["audit_next_focus"] = current.recommended_next_focus
        root_cause = current.blockers[0] if current.blockers else None
        return metadata, root_cause, constraints

    def render_doc_section(self, result: TaskAuditResult) -> str:
        lines = [
            "## 任务专项审计",
            "",
            "| 项目 | 值 |",
            "|------|------|",
            f"| Profile | {result.profile_id} |",
            f"| 摘要 | {result.summary} |",
        ]
        if result.score_total is not None:
            lines.append(f"| 评分 | {result.score_total} |")
        if result.recommended_next_focus:
            lines.append(f"| 下一步重点 | {result.recommended_next_focus} |")
        if result.dimensions:
            lines.extend(
                [
                    "",
                    "### 审计维度",
                    "",
                    "| 维度 | 值 |",
                    "|------|------|",
                ]
            )
            for key, value in sorted(result.dimensions.items()):
                lines.append(f"| {key} | {value} |")
        if result.blockers:
            lines.extend(["", "### 主要 Blocker", ""])
            lines.extend(f"- {item}" for item in result.blockers)
        if result.improvements:
            lines.extend(["", "### 最近改善", ""])
            lines.extend(f"- {item}" for item in result.improvements)
        if result.regressions:
            lines.extend(["", "### 回退风险", ""])
            lines.extend(f"- {item}" for item in result.regressions)
        return "\n".join(lines)


class TaskAuditRegistry:
    def __init__(self, plugin_paths: list[Path] | None = None) -> None:
        self._profiles: dict[str, TaskAuditProfile] = {}
        self._load_builtin_plugins()
        self._load_filesystem_plugins(plugin_paths or self._default_plugin_paths())

    def get(self, profile_id: str | None) -> TaskAuditProfile | None:
        if profile_id is None:
            return None
        return self._profiles.get(profile_id)

    def all_profiles(self) -> list[TaskAuditProfile]:
        return list(self._profiles.values())

    def resolve_for_run(self, metadata: dict[str, str]) -> TaskAuditProfile | None:
        return self.get(metadata.get("audit_profile"))

    def parse_result(self, metadata: dict[str, str], artifact_paths: dict[str, str]) -> TaskAuditResult | None:
        profile = self.resolve_for_run(metadata)
        if profile is None:
            return None
        preferred_path = artifact_paths.get("task_audit")
        if preferred_path is not None:
            return profile.parse_result(preferred_path)
        for path in artifact_paths.values():
            result = profile.parse_result(path)
            if result is not None:
                return result
        return None

    def build_audit_step(self, run_state_metadata: dict[str, str], test_command: str) -> Step | None:
        profile = self.resolve_for_run(run_state_metadata)
        if profile is None:
            return None
        return Step(
            kind=StepKind.RUN_TASK_AUDIT,
            title="Run Task Audit",
            goal="Execute the task-specific audit and produce a structured audit artifact.",
            inputs={
                "profile": profile.profile_id,
                "command": test_command,
                "artifact_name": profile.artifact_name,
            },
        )

    def _load_builtin_plugins(self) -> None:
        package_name = "mini_cc.harness.audit.plugins"
        package = importlib.import_module(package_name)
        package_path = getattr(package, "__path__", None)
        if package_path is None:
            return
        for module_info in pkgutil.iter_modules(package_path):
            module = importlib.import_module(f"{package_name}.{module_info.name}")
            self._register_from_module(module)

    def _load_filesystem_plugins(self, plugin_paths: list[Path]) -> None:
        for plugin_dir in plugin_paths:
            if not plugin_dir.is_dir():
                continue
            for path in sorted(plugin_dir.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                module_name = f"mini_cc_task_audit_plugin_{path.stem}"
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                self._register_from_module(module)

    def _register_from_module(self, module: object) -> None:
        register = getattr(module, "register", None)
        if not callable(register):
            return
        registered = register()
        profiles = registered if isinstance(registered, list) else [registered]
        for profile in profiles:
            if isinstance(profile, TaskAuditProfile) and profile.profile_id:
                self._profiles[profile.profile_id] = profile

    def _default_plugin_paths(self) -> list[Path]:
        return [Path.cwd() / ".mini_cc" / "task_audit_plugins"]

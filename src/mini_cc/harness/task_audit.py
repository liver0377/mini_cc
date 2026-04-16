from __future__ import annotations

import json
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


class MiniJQAuditProfile(TaskAuditProfile):
    profile_id = "mini_jq"
    display_name = "Mini jq"

    def parse_result(self, artifact_path: str) -> TaskAuditResult | None:
        path = Path(artifact_path)
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(loaded, dict):
            return None
        summary = loaded.get("summary")
        summary_text = "task audit completed"
        dimensions: dict[str, str] = {}
        if isinstance(summary, dict):
            total = summary.get("cases_total")
            passed = summary.get("cases_passed")
            failed = summary.get("cases_failed")
            if total is not None:
                dimensions["cases_total"] = str(total)
            if passed is not None:
                dimensions["cases_passed"] = str(passed)
            if failed is not None:
                dimensions["cases_failed"] = str(failed)
            if passed is not None and total is not None:
                summary_text = f"{passed}/{total} semantic cases passed"
        elif isinstance(summary, str) and summary.strip():
            summary_text = summary.strip()

        coverage = loaded.get("coverage")
        if isinstance(coverage, dict):
            for key, value in coverage.items():
                dimensions[f"coverage_{key}"] = str(value).lower() if isinstance(value, bool) else str(value)

        score_total = loaded.get("score_total")
        blockers = [str(item) for item in loaded.get("blockers", []) if str(item).strip()]
        regressions = [str(item) for item in loaded.get("regressions", []) if str(item).strip()]
        improvements = [str(item) for item in loaded.get("improvements", []) if str(item).strip()]
        next_focus = loaded.get("recommended_next_focus")
        return TaskAuditResult(
            profile_id=self.profile_id,
            summary=summary_text,
            score_total=score_total if isinstance(score_total, int) else None,
            dimensions=dimensions,
            blockers=blockers,
            regressions=regressions,
            improvements=improvements,
            recommended_next_focus=str(next_focus) if isinstance(next_focus, str) and next_focus.strip() else None,
            raw_artifact_path=str(path),
        )


class TaskAuditRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, TaskAuditProfile] = {
            "mini_jq": MiniJQAuditProfile(),
        }

    def get(self, profile_id: str | None) -> TaskAuditProfile | None:
        if profile_id is None:
            return None
        return self._profiles.get(profile_id)

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
        if profile.profile_id == "mini_jq":
            return Step(
                kind=StepKind.RUN_TASK_AUDIT,
                title="Run Task Audit",
                goal="Execute the task-specific audit and produce a structured audit artifact.",
                inputs={
                    "profile": profile.profile_id,
                    "command": test_command,
                    "artifact_name": "jq_audit.json",
                },
            )
        return None

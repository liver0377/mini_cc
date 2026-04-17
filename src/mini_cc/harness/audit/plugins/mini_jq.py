from __future__ import annotations

import json
from pathlib import Path

from mini_cc.harness.audit.core import TaskAuditProfile, TaskAuditResult

_MINI_JQ_AUDIT_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "task_audit" / "mini_jq.py"
_MINI_JQ_SCAFFOLD_DIR = str(Path(__file__).parent / "mini_jq" / "scaffold")


class MiniJQAuditProfile(TaskAuditProfile):
    profile_id = "mini_jq"
    display_name = "Mini jq"
    artifact_name = "jq_audit.json"
    keywords = ["mini-jq", "mini jq", "jq 子集"]
    description = (
        "Mini jq JSON 处理器语义审计：对比系统 jq 对输入执行 filter 表达式的输出，"
        "覆盖 identity、字段访问、管道、数组迭代等核心语义。"
    )
    scaffold_dir = _MINI_JQ_SCAFFOLD_DIR
    default_test_command = "uv run pytest -q"
    bootstrap_guidance = (
        "如果目标涉及 mini-jq，请确保项目最终提供可执行入口 `mini-jq`，并让基础测试与后续语义审计都能直接调用它。"
    )
    audit_command = f"uv run python {_MINI_JQ_AUDIT_SCRIPT}"

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


def register() -> TaskAuditProfile:
    return MiniJQAuditProfile()

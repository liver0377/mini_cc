from __future__ import annotations

import shutil
from pathlib import Path

from mini_cc.harness.audit import TaskAuditProfile, TaskAuditRegistry
from mini_cc.harness.models import Step, StepKind, WorkItem

BOOTSTRAP_FLOW_METADATA = "bootstrap_flow"
BOOTSTRAP_STEP_BUDGET_SECONDS = 900
EDIT_CODE_STEP_BUDGET_SECONDS = 900

_IGNORED_NAMES = {
    ".git",
    ".mini_cc",
    ".pytest_cache",
    "__pycache__",
    ".python-version",
    ".gitignore",
    "AGENTS.md",
    "README",
    "README.md",
    "LICENSE",
    "LICENSE.md",
}
_IGNORED_SUFFIXES = {".md", ".txt"}


def is_bootstrap_candidate(cwd: Path) -> bool:
    for path in cwd.iterdir():
        if _should_ignore_entry(path):
            continue
        return False
    return True


def prepare_run_request(
    user_text: str,
    mode: str,
    cwd: Path,
    registry: TaskAuditRegistry | None = None,
) -> tuple[list[Step], dict[str, str]]:
    if mode == "plan":
        return (
            [
                Step(
                    kind=StepKind.MAKE_PLAN,
                    title="Plan",
                    goal="为用户目标制定可执行计划",
                    inputs={"prompt": user_text},
                ),
                Step(
                    kind=StepKind.FINALIZE,
                    title="Summarize",
                    goal="总结计划、风险和下一步建议，直接回复用户。",
                ),
            ],
            {},
        )

    effective_registry = registry or TaskAuditRegistry()
    profile = _match_audit_profile(user_text, effective_registry)
    steps: list[Step] = []
    metadata = _build_metadata(profile)

    if is_bootstrap_candidate(cwd):
        _apply_scaffold(profile, cwd)
        audit_context = _build_audit_context(effective_registry)
        bootstrap_prompt = (
            "当前工作目录几乎为空。请先搭建一个最小可运行的项目骨架，至少包含："
            "依赖/项目配置、源码入口、测试目录、基础测试或验收脚本，以及后续实现需要的最小文件结构。"
            f"{profile.bootstrap_guidance if profile else ''}"
            f"{audit_context}"
            f"所有 bootstrap 工作都必须围绕这个目标展开：{user_text}"
        )
        steps.append(
            Step(
                kind=StepKind.BOOTSTRAP_PROJECT,
                title="Bootstrap",
                goal="为当前空仓库搭建最小可运行项目骨架",
                inputs={"prompt": bootstrap_prompt},
                budget_seconds=BOOTSTRAP_STEP_BUDGET_SECONDS,
                work_items=_bootstrap_work_items(user_text, bootstrap_prompt, profile),
            )
        )
        metadata[BOOTSTRAP_FLOW_METADATA] = "true"
        metadata.setdefault("test_command", profile.default_test_command if profile else "uv run pytest -q")

    steps.extend(
        [
            Step(
                kind=StepKind.ANALYZE_REPO,
                title="Analyze",
                goal="分析当前仓库，找出与目标最相关的文件、约束和风险",
            ),
            Step(
                kind=StepKind.EDIT_CODE,
                title="Execute",
                goal="根据目标实现代码",
                inputs={"prompt": user_text},
                budget_seconds=EDIT_CODE_STEP_BUDGET_SECONDS,
                work_items=_edit_work_items(user_text),
            ),
            Step(
                kind=StepKind.FINALIZE,
                title="Finalize",
                goal="总结已完成工作、未完成项、验证情况和剩余风险，直接回复用户。",
            ),
        ]
    )
    return steps, metadata


def _match_audit_profile(user_text: str, registry: TaskAuditRegistry) -> TaskAuditProfile | None:
    best_profile: TaskAuditProfile | None = None
    best_score = 0.0
    for profile in registry.all_profiles():
        score = profile.match_score(user_text)
        if score > best_score:
            best_score = score
            best_profile = profile
    if best_score > 0.0:
        return best_profile
    return None


def _build_metadata(profile: TaskAuditProfile | None) -> dict[str, str]:
    if profile is None:
        return {}
    metadata: dict[str, str] = {"audit_profile": profile.profile_id}
    if profile.audit_command:
        metadata["task_audit_command"] = profile.audit_command
    return metadata


def _build_audit_context(registry: TaskAuditRegistry) -> str:
    profiles = registry.all_profiles()
    if not profiles:
        return ""
    lines = ["\n\n## 当前系统支持的审计任务\n"]
    lines.append("| 任务 ID | 名称 | 关键词 | 说明 |")
    lines.append("|---------|------|--------|------|")
    for p in profiles:
        kw = "、".join(p.keywords) if p.keywords else "-"
        desc = p.description or "-"
        lines.append(f"| {p.profile_id} | {p.display_name} | {kw} | {desc} |")
    lines.append("")
    return "\n".join(lines)


def _apply_scaffold(profile: TaskAuditProfile | None, cwd: Path) -> None:
    if profile is None or profile.scaffold_dir is None:
        return
    scaffold_path = Path(profile.scaffold_dir)
    if not scaffold_path.is_dir():
        return
    for src in scaffold_path.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(scaffold_path)
        dest = cwd / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _should_ignore_entry(path: Path) -> bool:
    if path.name in _IGNORED_NAMES:
        return True
    if path.name.startswith(".") and path.name not in {".env", ".env.example"}:
        return True
    if path.is_file() and path.suffix.lower() in _IGNORED_SUFFIXES:
        return True
    return False


def _bootstrap_work_items(user_text: str, bootstrap_prompt: str, profile: TaskAuditProfile | None) -> list[WorkItem]:
    bootstrap_hint = profile.bootstrap_guidance if profile is not None else ""
    return [
        WorkItem(
            id="bootstrap.inspect_repo",
            kind="bootstrap.inspect_repo",
            title="Inspect Repo",
            goal="检查当前仓库状态，确认已有 scaffold、源码入口、测试入口和缺失部分。",
            role="analyzer",
            inputs={"prompt": bootstrap_prompt},
        ),
        WorkItem(
            id="bootstrap.detect_scaffold",
            kind="bootstrap.detect_scaffold",
            title="Detect Scaffold",
            goal=(
                "识别当前项目骨架是否已足够支撑后续实现。"
                "如果 scaffold 已经存在，明确指出可复用部分和仍需补齐的最小缺口。"
            ),
            role="analyzer",
            depends_on=["bootstrap.inspect_repo"],
            inputs={"prompt": bootstrap_prompt},
        ),
        WorkItem(
            id="bootstrap.generate_skeleton",
            kind="bootstrap.generate_skeleton",
            title="Generate Skeleton",
            goal=(
                "围绕目标生成最小可运行项目骨架方案，明确需要的源码、测试、配置和入口文件。"
                f"{bootstrap_hint} 用户目标：{user_text}"
            ),
            role="implementer",
            depends_on=["bootstrap.detect_scaffold"],
            inputs={"prompt": bootstrap_prompt},
        ),
        WorkItem(
            id="bootstrap.write_skeleton",
            kind="bootstrap.write_skeleton",
            title="Write Skeleton",
            goal="将项目骨架落盘，并确保目录结构、测试和入口文件可运行。",
            role="implementer",
            depends_on=["bootstrap.generate_skeleton"],
            inputs={"prompt": bootstrap_prompt},
        ),
        WorkItem(
            id="bootstrap.verify_bootstrap",
            kind="bootstrap.verify_bootstrap",
            title="Verify Bootstrap",
            goal="复核 bootstrap 结果，确认骨架已满足后续 analyze/edit 的最小前提，并指出剩余风险。",
            role="reporter",
            depends_on=["bootstrap.write_skeleton"],
            inputs={"prompt": bootstrap_prompt},
        ),
    ]


def _edit_work_items(user_text: str) -> list[WorkItem]:
    return [
        WorkItem(
            id="edit.select_target_slice",
            kind="edit.select_target_slice",
            title="Select Target Slice",
            goal=f"定位最相关的实现切片、文件边界和风险点。目标：{user_text}",
            role="analyzer",
            inputs={"prompt": user_text},
        ),
        WorkItem(
            id="edit.apply_patch_slice",
            kind="edit.apply_patch_slice",
            title="Apply Patch Slice",
            goal=f"在最小必要范围内完成实现或修复，并保持变更可验证。目标：{user_text}",
            role="implementer",
            depends_on=["edit.select_target_slice"],
            inputs={"prompt": user_text},
        ),
        WorkItem(
            id="edit.self_check",
            kind="edit.self_check",
            title="Self Check",
            goal="检查本轮修改是否覆盖目标、是否存在明显遗漏，以及后续验证应重点关注什么。",
            role="reporter",
            depends_on=["edit.apply_patch_slice"],
            inputs={"prompt": user_text},
        ),
        WorkItem(
            id="edit.emit_change_summary",
            kind="edit.emit_change_summary",
            title="Emit Change Summary",
            goal="总结本轮修改、影响范围和待验证项，为后续 run_tests / finalize 提供清晰上下文。",
            role="reporter",
            depends_on=["edit.self_check"],
            inputs={"prompt": user_text},
        ),
    ]

from __future__ import annotations

from pathlib import Path

from mini_cc.harness.models import Step, StepKind

BOOTSTRAP_FLOW_METADATA = "bootstrap_flow"
_MINI_JQ_AUDIT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "task_audit" / "mini_jq.py"
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


def prepare_run_request(user_text: str, mode: str, cwd: Path) -> tuple[list[Step], dict[str, str]]:
    if mode == "plan":
        return (
            [
                Step(
                    kind=StepKind.MAKE_PLAN,
                    title="Plan",
                    goal=f"为以下目标制定可执行计划：{user_text}",
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

    steps: list[Step] = []
    metadata = _task_metadata(user_text)
    if is_bootstrap_candidate(cwd):
        steps.append(
            Step(
                kind=StepKind.BOOTSTRAP_PROJECT,
                title="Bootstrap",
                goal=f"为当前空仓库搭建最小可运行项目骨架，使后续可以围绕以下目标持续实现与验证：{user_text}",
                inputs={
                    "prompt": (
                        "当前工作目录几乎为空。请先搭建一个最小可运行的项目骨架，至少包含："
                        "依赖/项目配置、源码入口、测试目录、基础测试或验收脚本，以及后续实现需要的最小文件结构。"
                        f"{_bootstrap_task_guidance(user_text)}"
                        f"所有 bootstrap 工作都必须围绕这个目标展开：{user_text}"
                    )
                },
            )
        )
        metadata = {
            **metadata,
            BOOTSTRAP_FLOW_METADATA: "true",
        }
        metadata.setdefault("test_command", "uv run pytest -q")

    steps.extend(
        [
            Step(
                kind=StepKind.ANALYZE_REPO,
                title="Analyze",
                goal=f"分析当前仓库，与以下目标最相关的文件、约束和风险：{user_text}",
            ),
            Step(
                kind=StepKind.EDIT_CODE,
                title="Execute",
                goal=user_text,
                inputs={"prompt": user_text},
            ),
            Step(
                kind=StepKind.FINALIZE,
                title="Finalize",
                goal="总结已完成工作、未完成项、验证情况和剩余风险，直接回复用户。",
            ),
        ]
    )
    return steps, metadata


def _should_ignore_entry(path: Path) -> bool:
    if path.name in _IGNORED_NAMES:
        return True
    if path.name.startswith(".") and path.name not in {".env", ".env.example"}:
        return True
    if path.is_file() and path.suffix.lower() in _IGNORED_SUFFIXES:
        return True
    return False


def _task_metadata(user_text: str) -> dict[str, str]:
    if _looks_like_mini_jq_task(user_text):
        return {
            "audit_profile": "mini_jq",
            "task_audit_command": f"uv run python {_MINI_JQ_AUDIT_SCRIPT}",
        }
    return {}


def _looks_like_mini_jq_task(user_text: str) -> bool:
    normalized = user_text.lower().replace("_", "-")
    return "mini-jq" in normalized or "mini jq" in normalized or "jq 子集" in user_text


def _bootstrap_task_guidance(user_text: str) -> str:
    if _looks_like_mini_jq_task(user_text):
        return (
            "如果目标涉及 mini-jq，请确保项目最终提供可执行入口 `mini-jq`，"
            "并让基础测试与后续语义审计都能直接调用它。"
        )
    return ""

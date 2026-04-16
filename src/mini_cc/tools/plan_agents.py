from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from mini_cc.tools.base import BaseTool, ToolResult

_DEFAULT_MAX_AGENTS = 4


class PlanAgentsInput(BaseModel):
    goal: str
    path: str | None = None
    max_agents: int = _DEFAULT_MAX_AGENTS
    include_tests: bool = True


class AgentDispatchItem(BaseModel):
    index: int
    scope: str
    mode: str
    prompt: str


class AgentDispatchPlan(BaseModel):
    goal: str
    root: str
    recommended_agent_count: int
    dispatch_plan: list[AgentDispatchItem] = Field(default_factory=list)
    overflow_scopes: list[str] = Field(default_factory=list)


class PlanAgentsTool(BaseTool):
    @property
    def name(self) -> str:
        return "plan_agents"

    @property
    def description(self) -> str:
        return "基于目录结构生成 JSON 格式的 sub-agent 派工建议，帮助主 Agent 在创建子 Agent 前先完成模块拆分"

    @property
    def input_schema(self) -> type[BaseModel]:
        return PlanAgentsInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = PlanAgentsInput.model_validate(kwargs)
        root = Path(parsed.path or ".").resolve()
        if not root.exists():
            return ToolResult(error=f"路径不存在: {root}", success=False)
        if not root.is_dir():
            return ToolResult(error=f"不是目录: {root}", success=False)

        candidates = self._candidate_groups(root, parsed.include_tests)
        if not candidates:
            candidates = [root]

        selected = candidates[: max(1, parsed.max_agents)]
        plan = AgentDispatchPlan(
            goal=parsed.goal,
            root=str(root),
            recommended_agent_count=len(selected),
        )
        for index, group in enumerate(selected, start=1):
            rel = group.relative_to(root) if group != root else Path(".")
            label = str(rel)
            prompt = self._prompt(parsed.goal, label)
            plan.dispatch_plan.append(
                AgentDispatchItem(
                    index=index,
                    scope=label,
                    mode="readonly",
                    prompt=prompt,
                )
            )

        leftovers = candidates[len(selected) :]
        if leftovers:
            plan.overflow_scopes = [str(path.relative_to(root)) for path in leftovers[:8]]

        return ToolResult(output=plan.model_dump_json(indent=2))

    def _candidate_groups(self, root: Path, include_tests: bool) -> list[Path]:
        preferred_names = [
            "src",
            "mini_cc",
            "app",
            "lib",
            "packages",
            "tests",
            "docs",
            "scripts",
        ]
        top_level = self._visible_dirs(root)
        by_name = {path.name: path for path in top_level}

        groups: list[Path] = []
        for name in preferred_names:
            if name == "tests" and not include_tests:
                continue
            path = by_name.get(name)
            if path is not None:
                groups.append(path)

        remaining = [path for path in top_level if path not in groups and (include_tests or path.name != "tests")]
        groups.extend(remaining)

        src_like = by_name.get("src")
        if src_like is not None:
            nested = self._visible_dirs(src_like)
            if nested:
                promoted = [path for path in nested if path.name not in {"__pycache__"}]
                if promoted:
                    groups = promoted + [path for path in groups if path != src_like]

        return groups

    def _visible_dirs(self, root: Path) -> list[Path]:
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            return []
        return [entry for entry in entries if entry.is_dir() and not entry.name.startswith(".")]

    def _prompt(self, goal: str, scope: str) -> str:
        return (
            f"聚焦 `{scope}` 范围，围绕以下目标工作：{goal}。"
            "先阅读该范围内的关键文件，再总结职责、风险、相关依赖和建议的下一步。"
        )

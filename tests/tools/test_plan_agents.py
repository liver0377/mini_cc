from __future__ import annotations

import json
from pathlib import Path

from mini_cc.tools.plan_agents import PlanAgentsTool


class TestPlanAgentsTool:
    def test_plan_agents_prefers_nested_src_modules(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "agent").mkdir()
        (tmp_path / "src" / "tui").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "docs").mkdir()

        tool = PlanAgentsTool()
        result = tool.execute(goal="分析项目架构", path=str(tmp_path), max_agents=3)
        payload = json.loads(result.output)

        assert result.success is True
        assert payload["recommended_agent_count"] == 3
        scopes = [item["scope"] for item in payload["dispatch_plan"]]
        assert "src/agent" in scopes
        assert "src/tui" in scopes
        assert all("prompt" in item for item in payload["dispatch_plan"])

    def test_plan_agents_handles_missing_path(self, tmp_path: Path) -> None:
        tool = PlanAgentsTool()
        result = tool.execute(goal="分析", path=str(tmp_path / "missing"))

        assert result.success is False
        assert "路径不存在" in result.error

    def test_plan_agents_can_skip_tests(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()

        tool = PlanAgentsTool()
        result = tool.execute(goal="分析", path=str(tmp_path), max_agents=4, include_tests=False)
        payload = json.loads(result.output)

        assert result.success is True
        scopes = [item["scope"] for item in payload["dispatch_plan"]]
        assert "tests" not in scopes

    def test_plan_agents_emits_json_shape(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "core").mkdir()

        tool = PlanAgentsTool()
        result = tool.execute(goal="理解核心模块", path=str(tmp_path), max_agents=2)
        payload = json.loads(result.output)

        assert result.success is True
        assert set(payload) == {
            "goal",
            "root",
            "recommended_agent_count",
            "dispatch_plan",
            "overflow_scopes",
        }
        assert isinstance(payload["dispatch_plan"], list)
        assert payload["dispatch_plan"][0]["mode"] == "readonly"

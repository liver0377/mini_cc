from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult


class GlobInput(BaseModel):
    pattern: str
    path: str | None = None


class GlobTool(BaseTool):
    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "按文件名模式搜索文件（基于 ripgrep）"

    @property
    def input_schema(self) -> type[BaseModel]:
        return GlobInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = GlobInput.model_validate(kwargs)
        search_path = parsed.path or "."

        cmd: list[str] = ["rg", "--files", "--glob", parsed.pattern, search_path]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return ToolResult(error="ripgrep (rg) 未安装，请先安装 ripgrep", success=False)
        except subprocess.TimeoutExpired:
            return ToolResult(error="搜索超时", success=False)

        if result.returncode == 2:
            return ToolResult(error=result.stderr.strip() or "搜索出错", success=False)

        matches = result.stdout.strip()
        if not matches:
            return ToolResult(output="未找到匹配的文件")

        return ToolResult(output=matches)

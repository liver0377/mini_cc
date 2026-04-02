from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult


class GrepInput(BaseModel):
    pattern: str
    include: str | None = None
    path: str | None = None


class GrepTool(BaseTool):
    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return "按正则表达式搜索代码内容（基于 ripgrep）"

    @property
    def input_schema(self) -> type[BaseModel]:
        return GrepInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = GrepInput.model_validate(kwargs)
        search_path = parsed.path or "."

        cmd: list[str] = ["rg", "--line-number", "--no-heading", "--with-filename", "--color=never", parsed.pattern]
        if parsed.include:
            cmd.extend(["--glob", parsed.include])
        cmd.append(search_path)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return ToolResult(error="ripgrep (rg) 未安装，请先安装 ripgrep", success=False)
        except subprocess.TimeoutExpired:
            return ToolResult(error="搜索超时", success=False)

        if result.returncode == 2:
            error_msg = result.stderr.strip()
            if "unrecognized" in error_msg or "invalid" in error_msg.lower():
                return ToolResult(error=f"正则表达式无效: {error_msg}", success=False)
            return ToolResult(error=error_msg or "搜索出错", success=False)

        if result.returncode == 1:
            return ToolResult(output="未找到匹配的内容")

        return ToolResult(output=result.stdout.strip())

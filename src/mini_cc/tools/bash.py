from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult


class BashInput(BaseModel):
    command: str
    timeout: int = 120000


class Bash(BaseTool):
    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "执行 shell 命令"

    @property
    def input_schema(self) -> type[BaseModel]:
        return BashInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = BashInput.model_validate(kwargs)

        try:
            result = subprocess.run(
                parsed.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=parsed.timeout / 1000,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(error=f"命令超时 ({parsed.timeout}ms)", success=False)
        except OSError as e:
            return ToolResult(error=f"执行失败: {e}", success=False)

        output = result.stdout
        if result.stderr:
            output = f"{output}\nstderr:\n{result.stderr}" if output else f"stderr:\n{result.stderr}"

        if result.returncode != 0:
            return ToolResult(
                output=output.strip(),
                error=f"命令退出码: {result.returncode}",
                success=False,
            )

        return ToolResult(output=output.strip())

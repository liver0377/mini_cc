from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult


class FileReadInput(BaseModel):
    file_path: str


class FileRead(BaseTool):
    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return "读取文本文件内容"

    @property
    def input_schema(self) -> type[BaseModel]:
        return FileReadInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = FileReadInput.model_validate(kwargs)
        path = Path(parsed.file_path)

        if not path.exists():
            return ToolResult(error=f"文件不存在: {path}", success=False)
        if not path.is_file():
            return ToolResult(error=f"路径不是文件: {path}", success=False)

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(error=f"文件非文本或编码不支持: {path}", success=False)
        except OSError as e:
            return ToolResult(error=f"读取失败: {e}", success=False)

        return ToolResult(output=content)

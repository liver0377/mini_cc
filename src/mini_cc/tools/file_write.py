from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult


class FileWriteInput(BaseModel):
    file_path: str
    content: str


class FileWrite(BaseTool):
    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return "将内容写入文件（全量覆盖）"

    @property
    def input_schema(self) -> type[BaseModel]:
        return FileWriteInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = FileWriteInput.model_validate(kwargs)
        path = Path(parsed.file_path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(parsed.content, encoding="utf-8")
        except OSError as e:
            return ToolResult(error=f"写入失败: {e}", success=False)

        return ToolResult(output="文件写入成功")

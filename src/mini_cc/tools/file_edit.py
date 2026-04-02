from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult


class FileEditInput(BaseModel):
    file_path: str
    old_string: str
    new_string: str


class FileEdit(BaseTool):
    @property
    def name(self) -> str:
        return "file_edit"

    @property
    def description(self) -> str:
        return "通过字符串替换编辑文件"

    @property
    def input_schema(self) -> type[BaseModel]:
        return FileEditInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = FileEditInput.model_validate(kwargs)
        path = Path(parsed.file_path)

        if not path.exists():
            return ToolResult(error=f"文件不存在: {path}", success=False)
        if not path.is_file():
            return ToolResult(error=f"路径不是文件: {path}", success=False)

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult(error=f"读取失败: {e}", success=False)

        count = content.count(parsed.old_string)
        if count == 0:
            return ToolResult(error="未找到匹配的字符串", success=False)
        if count > 1:
            return ToolResult(error=f"找到 {count} 处匹配，请提供更多上下文以唯一确定替换位置", success=False)

        new_content = content.replace(parsed.old_string, parsed.new_string, 1)

        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return ToolResult(error=f"写入失败: {e}", success=False)

        return ToolResult(output="文件编辑成功")

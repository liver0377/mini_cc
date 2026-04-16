from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mini_cc.tools.base import BaseTool, ToolResult

_DEFAULT_MAX_DEPTH = 2
_DEFAULT_MAX_ENTRIES = 60


class ScanDirInput(BaseModel):
    path: str | None = None
    max_depth: int = _DEFAULT_MAX_DEPTH
    max_entries: int = _DEFAULT_MAX_ENTRIES
    include_hidden: bool = False


class ScanDirTool(BaseTool):
    @property
    def name(self) -> str:
        return "scan_dir"

    @property
    def description(self) -> str:
        return "扫描目录结构并输出分层摘要，适合在派发 sub-agent 前快速识别模块边界"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ScanDirInput

    def execute(self, **kwargs: Any) -> ToolResult:
        parsed = ScanDirInput.model_validate(kwargs)
        root = Path(parsed.path or ".").resolve()
        if not root.exists():
            return ToolResult(error=f"路径不存在: {root}", success=False)
        if not root.is_dir():
            return ToolResult(error=f"不是目录: {root}", success=False)

        try:
            lines = self._scan(root, parsed.max_depth, parsed.max_entries, parsed.include_hidden)
        except OSError as err:
            return ToolResult(error=f"扫描目录失败: {err}", success=False)

        return ToolResult(output="\n".join(lines))

    def _scan(self, root: Path, max_depth: int, max_entries: int, include_hidden: bool) -> list[str]:
        lines = [f"root: {root}"]
        total_dirs = 0
        total_files = 0
        emitted = 0

        def _walk(directory: Path, depth: int) -> None:
            nonlocal total_dirs, total_files, emitted
            if depth > max_depth or emitted >= max_entries:
                return

            try:
                entries = sorted(directory.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))
            except OSError:
                lines.append(f"{'  ' * depth}- [unreadable] {directory.name}/")
                emitted += 1
                return

            visible = [entry for entry in entries if include_hidden or not entry.name.startswith(".")]
            dirs = [entry for entry in visible if entry.is_dir()]
            files = [entry for entry in visible if entry.is_file()]

            if depth > 0:
                lines.append(
                    f"{'  ' * (depth - 1)}- {directory.name}/"
                    f" [dirs={len(dirs)}, files={len(files)}]"
                )
                emitted += 1
                if emitted >= max_entries:
                    return

            total_dirs += len(dirs)
            total_files += len(files)

            preview_names = [entry.name + ("/" if entry.is_dir() else "") for entry in visible[:8]]
            if depth > 0 and preview_names and emitted < max_entries:
                lines.append(f"{'  ' * depth}contains: {', '.join(preview_names)}")
                emitted += 1

            for child in dirs:
                if emitted >= max_entries:
                    break
                _walk(child, depth + 1)

        _walk(root, 0)
        lines.append(f"summary: dirs={total_dirs}, files={total_files}, max_depth={max_depth}, emitted={emitted}")
        if emitted >= max_entries:
            lines.append("note: output truncated by max_entries")
        return lines

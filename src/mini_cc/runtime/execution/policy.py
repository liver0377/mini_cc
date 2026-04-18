from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mini_cc.tools import READONLY_TOOL_NAMES

_WRITE_TOOLS = frozenset({"file_edit", "file_write"})
_PATH_ARG_BY_TOOL = {
    "file_read": "file_path",
    "file_edit": "file_path",
    "file_write": "file_path",
    "glob": "path",
    "grep": "path",
    "scan_dir": "path",
}


@dataclass(frozen=True)
class ExecutionPolicy:
    readonly: bool = False
    allowed_tools: frozenset[str] | None = None
    scope_paths: list[str] = field(default_factory=lambda: ["."])
    workspace_root: str = ""
    allow_bash: bool = True

    def is_tool_allowed(self, tool_name: str) -> tuple[bool, str]:
        if self.readonly and tool_name not in READONLY_TOOL_NAMES:
            return False, f"只读 Agent 不允许使用 {tool_name} 工具"
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            return False, f"工具 {tool_name} 不在允许列表中"
        if tool_name == "bash" and not self.allow_bash:
            return False, "当前执行策略不允许使用 bash 工具"
        return True, ""

    def is_path_in_scope(self, file_path: str) -> tuple[bool, str]:
        if not self.scope_paths:
            return True, ""
        if "." in self.scope_paths:
            return True, ""
        target = Path(file_path)
        if not target.is_absolute() and self.workspace_root:
            target = Path(self.workspace_root) / target
        try:
            target = target.resolve()
        except OSError:
            return False, f"无法解析路径: {file_path}"
        for scope in self.scope_paths:
            scope_path = Path(scope)
            if not scope_path.is_absolute() and self.workspace_root:
                scope_path = Path(self.workspace_root) / scope_path
            try:
                scope_path = scope_path.resolve()
            except OSError:
                continue
            try:
                target.relative_to(scope_path)
                return True, ""
            except ValueError:
                continue
        return False, f"路径 {file_path} 不在允许的 scope 范围内: {self.scope_paths}"

    def validate_tool_call(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
        allowed, reason = self.is_tool_allowed(tool_name)
        if not allowed:
            return False, reason
        path_arg = _PATH_ARG_BY_TOOL.get(tool_name)
        if path_arg is not None:
            raw_path = args.get(path_arg, "")
            if raw_path:
                in_scope, reason = self.is_path_in_scope(str(raw_path))
                if not in_scope:
                    return False, reason
        if tool_name == "bash" and self._has_restricted_scope:
            return False, "声明了受限 scope 的 Agent 不允许使用 bash；请改用受约束的文件工具"
        return True, ""

    @property
    def _has_restricted_scope(self) -> bool:
        return bool(self.scope_paths) and "." not in self.scope_paths

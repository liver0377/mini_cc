from __future__ import annotations

from mini_cc.tools.base import BaseTool, ToolRegistry, ToolResult
from mini_cc.tools.bash import Bash
from mini_cc.tools.file_edit import FileEdit
from mini_cc.tools.file_read import FileRead
from mini_cc.tools.file_write import FileWrite
from mini_cc.tools.glob import GlobTool
from mini_cc.tools.grep import GrepTool
from mini_cc.tools.plan_agents import PlanAgentsTool
from mini_cc.tools.scan_dir import ScanDirTool

READONLY_TOOL_NAMES = frozenset({"file_read", "glob", "grep", "scan_dir", "plan_agents"})


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FileRead())
    registry.register(FileEdit())
    registry.register(FileWrite())
    registry.register(Bash())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ScanDirTool())
    registry.register(PlanAgentsTool())
    return registry


def create_readonly_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FileRead())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ScanDirTool())
    registry.register(PlanAgentsTool())
    return registry


__all__ = [
    "BaseTool",
    "ToolRegistry",
    "ToolResult",
    "READONLY_TOOL_NAMES",
    "FileRead",
    "FileEdit",
    "FileWrite",
    "Bash",
    "GlobTool",
    "GrepTool",
    "PlanAgentsTool",
    "ScanDirTool",
    "create_default_registry",
    "create_readonly_registry",
]

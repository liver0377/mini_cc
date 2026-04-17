from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from textual.widgets import Static


@dataclass
class _ToolEntry:
    name: str
    status: str = "running"
    success: bool = False
    preview: str = ""


_RUNNING_BRIGHT = "#ffcc00"
_RUNNING_DIM = "#665500"
_SUCCESS = "#238636"
_FAIL = "#da3633"
_TOOL_NAME_COLOR = "#58a6ff"

_BLINK_INTERVAL = 0.30


class AgentToolStrip(Static):
    DEFAULT_CSS = """
    AgentToolStrip {
        padding: 0 4;
        margin: 0 0;
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self, agent_id: str, color: str) -> None:
        super().__init__("", markup=True)
        self._agent_id = agent_id
        self._color = color
        self._tools: list[_ToolEntry] = []
        self._blink_on = True
        self._completed = False
        self._timer: Any = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(_BLINK_INTERVAL, self._tick)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _tick(self) -> None:
        if self._completed:
            return
        if any(t.status == "running" for t in self._tools):
            self._blink_on = not self._blink_on
            self._refresh()

    def add_tool(self, name: str) -> None:
        self._tools.append(_ToolEntry(name=name))
        self._refresh()

    def complete_tool(self, name: str, success: bool, preview: str = "") -> None:
        for tool in reversed(self._tools):
            if tool.name == name and tool.status == "running":
                tool.status = "done"
                tool.success = success
                tool.preview = preview
                break
        self._refresh()

    def finalize(self) -> None:
        self._completed = True
        self._blink_on = True
        self._refresh()

    def _refresh(self) -> None:
        parts: list[str] = []
        for tool in self._tools:
            if tool.status == "running":
                icon = "⚡" if self._blink_on else "·"
                color = _RUNNING_BRIGHT if self._blink_on else _RUNNING_DIM
                parts.append(f"[{color}]{icon}[/{color}] [{_TOOL_NAME_COLOR}]{tool.name}[/]")
            else:
                marker = f"[bold {_SUCCESS}]✓[/]" if tool.success else f"[bold {_FAIL}]✗[/]"
                parts.append(f"{marker} [{_TOOL_NAME_COLOR}]{tool.name}[/]")

        tools_line = "  ".join(parts)
        suffix = ""
        if self._completed and self._tools:
            succeeded = sum(1 for t in self._tools if t.success)
            total = len(self._tools)
            suffix = f"  [dim]({succeeded}/{total} 成功)[/]"

        self.update(f"    [{self._color}]{self._agent_id}[/][dim] ▸ [/]{tools_line}{suffix}")

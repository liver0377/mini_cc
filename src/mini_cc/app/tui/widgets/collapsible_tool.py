from __future__ import annotations

from rich.markup import escape
from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, Static

_MAX_COLLAPSED_PREVIEW = 120


class CollapsibleTool(Collapsible):
    DEFAULT_CSS = """
    CollapsibleTool {
        width: 1fr;
        margin: 0 1;
        padding: 0;
    }
    CollapsibleTool > CollapsibleTitle {
        color: $text-muted;
        padding: 0 1;
        background: transparent;
    }
    CollapsibleTool > CollapsibleContents {
        padding: 0 1;
        margin: 0 0 0 2;
        background: $boost;
        border-left: tall $accent;
    }
    """

    def __init__(self, tool_name: str, output: str, success: bool) -> None:
        icon = "✓" if success else "✗"
        preview = output[:_MAX_COLLAPSED_PREVIEW]
        if len(output) > _MAX_COLLAPSED_PREVIEW:
            preview += "…"
        title_str = f"{icon} {tool_name}  {escape(preview)}"
        content = self._build_content(output)
        super().__init__(
            content,
            title=title_str,
            collapsed=True,
            classes="tool-call",
        )

    @staticmethod
    def _build_content(output: str) -> Widget:
        if "\n" in output:
            return Markdown(f"```\n{output}\n```")
        return Static(output, markup=False)

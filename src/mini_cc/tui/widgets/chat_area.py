from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static
from textual.widgets._markdown import MarkdownStream


class ChatArea(VerticalScroll):
    DEFAULT_CSS = """
    ChatArea {
        height: 1fr;
        width: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0 1;
        scrollbar-size: 1 1;
    }

    ChatArea .user-msg {
        background: $boost;
        color: $text;
        padding: 1 2;
        margin: 1 0;
        width: 1fr;
    }

    ChatArea .ai-msg {
        padding: 0 2;
        margin: 1 0;
        width: 1fr;
    }

    ChatArea .tool-msg {
        color: $text-muted;
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }

    ChatArea .system-msg {
        color: $text-muted;
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._current_stream: MarkdownStream | None = None

    async def add_user_message(self, text: str) -> None:
        widget = Static(f"[bold green]❯[/] {text}", classes="user-msg", markup=True)
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def begin_assistant_message(self) -> None:
        md = Markdown(classes="ai-msg")
        await self.mount(md)
        self._current_stream = Markdown.get_stream(md)
        self.scroll_end(animate=False)

    async def append_assistant_text(self, text: str) -> None:
        if self._current_stream is not None:
            await self._current_stream.write(text)
            self.scroll_end(animate=False)

    async def end_assistant_message(self) -> None:
        if self._current_stream is not None:
            await self._current_stream.stop()
            self._current_stream = None

    async def add_tool_call(self, name: str) -> None:
        widget = Static(f"  ⚙ [bold cyan]{name}[/]...", classes="tool-msg", markup=True)
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_tool_result(self, name: str, output: str, success: bool) -> None:
        marker = "[bold green]✓[/]" if success else "[bold red]✗[/]"
        preview = output[:200] + "..." if len(output) > 200 else output
        widget = Static(f"  {marker} [cyan]{name}[/]: {preview}", classes="tool-msg", markup=True)
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_system_message(self, text: str) -> None:
        widget = Static(text, classes="system-msg", markup=True)
        await self.mount(widget)
        self.scroll_end(animate=False)

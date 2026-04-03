from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static
from textual.widgets._markdown import MarkdownStream

_AGENT_COLORS = ["cyan", "magenta", "yellow", "green", "blue", "red"]


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

    ChatArea .agent-label {
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }

    ChatArea .agent-msg {
        color: $text-muted;
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }

    ChatArea .agent-tool-msg {
        color: $text-muted;
        padding: 0 4;
        margin: 0 0;
        width: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._current_stream: MarkdownStream | None = None
        self._agent_color_index: dict[str, str] = {}

    def _agent_color(self, agent_id: str) -> str:
        if agent_id not in self._agent_color_index:
            idx = len(self._agent_color_index) % len(_AGENT_COLORS)
            self._agent_color_index[agent_id] = _AGENT_COLORS[idx]
        return self._agent_color_index[agent_id]

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

    async def add_agent_start(self, agent_id: str, task_id: int, prompt: str) -> None:
        color = self._agent_color(agent_id)
        widget = Static(
            f"  🤖 [bold {color}]子 Agent {agent_id}[/][dim] (Task #{task_id})[/] 启动\n    [dim]{prompt}[/]",
            classes="agent-msg",
            markup=True,
        )
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_agent_tool_call(self, agent_id: str, tool_name: str) -> None:
        color = self._agent_color(agent_id)
        widget = Static(
            f"    ⚙ [{color}]{agent_id}[/][dim] ▸ [/][bold cyan]{tool_name}[/][dim](...)[/]",
            classes="agent-tool-msg",
            markup=True,
        )
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_agent_tool_result(self, agent_id: str, tool_name: str, success: bool, output_preview: str) -> None:
        color = self._agent_color(agent_id)
        marker = "[bold green]✓[/]" if success else "[bold red]✗[/]"
        preview = output_preview[:100] + "..." if len(output_preview) > 100 else output_preview
        widget = Static(
            f"    {marker} [{color}]{agent_id}[/][dim] ▸ [/][cyan]{tool_name}[/]: {preview}",
            classes="agent-tool-msg",
            markup=True,
        )
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_agent_notification(self, *, agent_id: str, task_id: int, success: bool, output: str) -> None:
        color = self._agent_color(agent_id)
        marker = "[bold green]✓[/]" if success else "[bold red]✗[/]"
        status_text = "完成" if success else "失败"
        preview = output[:80] + ("..." if len(output) > 80 else "")
        widget = Static(
            f"  {marker} [bold {color}]子 Agent {agent_id}[/][dim] (Task #{task_id})[/] {status_text}\n"
            f"    [dim]{preview}[/]",
            classes="agent-msg",
            markup=True,
        )
        await self.mount(widget)
        self.scroll_end(animate=False)

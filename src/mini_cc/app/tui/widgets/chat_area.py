from __future__ import annotations

from rich.markup import escape
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static
from textual.widgets._markdown import MarkdownStream

from mini_cc.app.tui.theme import DEFAULT_THEME
from mini_cc.app.tui.widgets.agent_tool_strip import AgentToolStrip
from mini_cc.app.tui.widgets.collapsible_tool import CollapsibleTool

_T = DEFAULT_THEME

_AGENT_COLORS = list(_T.agent_colors)


class ChatArea(VerticalScroll):
    DEFAULT_CSS = f"""
    ChatArea {{
        height: 1fr;
        width: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0 2;
        scrollbar-size: 1 1;
        background: $surface;
    }}

    ChatArea .user-msg {{
        background: {_T.user_bg};
        border-left: tall {_T.user_border};
        color: $text;
        padding: 0 2;
        margin: 1 0;
        width: 1fr;
    }}

    ChatArea .ai-msg {{
        padding: 0 2;
        margin: 1 0;
        width: 1fr;
    }}

    ChatArea .ai-label {{
        color: {_T.assistant_label};
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }}

    ChatArea .tool-call-msg {{
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }}

    ChatArea .system-msg {{
        color: {_T.system_muted};
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }}

    ChatArea .agent-msg {{
        padding: 0 2;
        margin: 0 0;
        width: 1fr;
    }}

    ChatArea .done-marker {{
        color: {_T.separator};
        padding: 1 2;
        margin: 0 0;
        width: 1fr;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._current_stream: MarkdownStream | None = None
        self._agent_color_index: dict[str, str] = {}
        self._agent_strips: dict[str, AgentToolStrip] = {}

    def _agent_color(self, agent_id: str) -> str:
        if agent_id not in self._agent_color_index:
            idx = len(self._agent_color_index) % len(_AGENT_COLORS)
            self._agent_color_index[agent_id] = _AGENT_COLORS[idx]
        return self._agent_color_index[agent_id]

    async def add_user_message(self, text: str) -> None:
        widget = Static(f"[bold {_T.user_accent}]❯[/] {escape(text)}", classes="user-msg", markup=True)
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def begin_assistant_message(self) -> None:
        label = Static("Assistant", classes="ai-label", markup=False)
        md = Markdown(classes="ai-msg")
        await self.mount(label)
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
        widget = Static(
            f"  ⚙ [#58a6ff]{name}[/][dim](...)[/]",
            classes="tool-call-msg",
            markup=True,
        )
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_tool_result(self, name: str, output: str, success: bool) -> None:
        tool = CollapsibleTool(tool_name=name, output=output, success=success)
        await self.mount(tool)
        self.scroll_end(animate=False)

    async def add_system_message(self, text: str) -> None:
        widget = Static(text, classes="system-msg", markup=True)
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_agent_start(self, agent_id: str, task_id: int, prompt: str) -> None:
        color = self._agent_color(agent_id)
        widget = Static(
            f"  🤖 [bold {color}]子 Agent {agent_id}[/][dim] (Task #{task_id})[/] 启动\n    [dim]{escape(prompt)}[/]",
            classes="agent-msg",
            markup=True,
        )
        await self.mount(widget)
        strip = AgentToolStrip(agent_id, color)
        await self.mount(strip)
        self._agent_strips[agent_id] = strip
        self.scroll_end(animate=False)

    async def add_agent_tool_call(self, agent_id: str, tool_name: str) -> None:
        strip = self._agent_strips.get(agent_id)
        if strip is not None:
            strip.add_tool(tool_name)
        self.scroll_end(animate=False)

    async def add_agent_tool_result(self, agent_id: str, tool_name: str, success: bool, output_preview: str) -> None:
        strip = self._agent_strips.get(agent_id)
        if strip is not None:
            strip.complete_tool(tool_name, success, output_preview)
        self.scroll_end(animate=False)

    async def add_agent_notification(self, *, agent_id: str, task_id: int, success: bool, output: str) -> None:
        strip = self._agent_strips.pop(agent_id, None)
        if strip is not None:
            strip.finalize()

        color = self._agent_color(agent_id)
        marker = f"[bold {_T.tool_success}]✓[/]" if success else f"[bold {_T.tool_fail}]✗[/]"
        status_text = "完成" if success else "失败"
        preview = escape(output[:200] + ("…" if len(output) > 200 else ""))
        widget = Static(
            f"  {marker} [bold {color}]子 Agent {agent_id}[/][dim] (Task #{task_id})[/] {status_text}\n"
            f"    [dim]{preview}[/]",
            classes="agent-msg",
            markup=True,
        )
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def add_done_marker(self) -> None:
        widget = Static("───────────", classes="done-marker", markup=False)
        await self.mount(widget)
        self.scroll_end(animate=False)

    async def clear_messages(self) -> None:
        self._agent_strips.clear()
        for child in list(self.children):
            await child.remove()

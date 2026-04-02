from __future__ import annotations

from textual.events import Key
from textual.message import Message
from textual.widgets import TextArea


class InputArea(TextArea):
    DEFAULT_CSS = """
    InputArea {
        dock: bottom;
        height: auto;
        max-height: 8;
        min-height: 3;
        width: 1fr;
        margin: 0 1;
        border: tall $primary;
        padding: 0 1;
    }
    InputArea:focus {
        border: tall $accent;
    }
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self) -> None:
        super().__init__(
            text="",
            soft_wrap=True,
            tab_behavior="indent",
            show_line_numbers=False,
            compact=True,
            placeholder="输入消息... (Enter 发送, Shift+Enter 换行, Tab 切换模式)",
        )

    async def _on_key(self, event: Key) -> None:
        if event.key == "tab":
            event.prevent_default()
            toggle = getattr(self.screen, "action_toggle_mode", None)
            if toggle is not None:
                toggle()
            return
        if event.key == "enter":
            event.prevent_default()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
                self.text = ""
                self.cursor_location = (0, 0)
            return
        if event.key == "shift+enter":
            self.insert("\n")
            event.prevent_default()
            return
        await super()._on_key(event)

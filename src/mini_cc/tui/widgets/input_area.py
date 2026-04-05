from __future__ import annotations

from textual.binding import Binding
from textual.events import Key
from textual.message import Message
from textual.widgets import TextArea

from mini_cc.tui.widgets.completion_popup import CompletionPopup


class InputArea(TextArea):
    BINDINGS = [
        Binding("ctrl+c", "app.exit", "退出", show=False),
    ]

    DEFAULT_CSS = """
    InputArea {
        dock: bottom;
        height: auto;
        max-height: 8;
        min-height: 3;
        width: 1fr;
        margin: 0 2;
        border: tall #30363d;
        padding: 0 1;
        background: #0d1117;
    }
    InputArea:focus {
        border: tall #58a6ff;
    }
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class ExitRequested(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            text="",
            soft_wrap=True,
            tab_behavior="indent",
            show_line_numbers=False,
            compact=True,
            placeholder="输入消息... (Enter 发送, Shift+Enter 换行)",
        )
        self._history: list[str] = []
        self._history_idx: int = -1
        self._draft: str = ""
        self._completion_mode: str | None = None

    def _get_popup(self) -> CompletionPopup | None:
        try:
            return self.screen.query_one("#completion-popup", CompletionPopup)
        except Exception:
            return None

    def _is_completion_active(self) -> bool:
        popup = self._get_popup()
        return popup is not None and popup.popup_visible

    async def _on_key(self, event: Key) -> None:
        popup = self._get_popup()

        if self._is_completion_active() and popup is not None:
            if event.key in ("up", "ctrl+p"):
                event.prevent_default()
                if popup.highlighted is not None and popup.highlighted > 0:
                    popup.highlighted = popup.highlighted - 1
                return
            if event.key in ("down", "ctrl+n"):
                event.prevent_default()
                if popup.highlighted is not None:
                    popup.highlighted = min(popup.highlighted + 1, popup.option_count - 1)
                return
            if event.key in ("enter", "tab"):
                event.prevent_default()
                selected = popup.select_current()
                if selected is not None:
                    await self._insert_completion(selected)
                popup.hide()
                self._completion_mode = None
                return
            if event.key == "escape":
                event.prevent_default()
                popup.hide()
                self._completion_mode = None
                return
            if event.key in ("backspace", "left", "right"):
                await super()._on_key(event)
                self._dismiss_if_invalid()
                await self._check_completion_trigger()
                return

        if event.key == "tab":
            event.prevent_default()
            toggle = getattr(self.screen, "action_toggle_mode", None)
            if toggle is not None:
                toggle()
            return

        if event.key == "ctrl+p":
            event.prevent_default()
            self._completion_mode = "slash"
            if popup is not None:
                popup.show_slash_commands("/")
            return

        if event.key == "enter":
            event.prevent_default()
            text = self.text.strip()
            if text:
                self._history.append(text)
                self._history_idx = len(self._history)
                self.post_message(self.Submitted(text))
                self.text = ""
                self.cursor_location = (0, 0)
            return

        if event.key == "shift+enter":
            self.insert("\n")
            event.prevent_default()
            return

        if event.key == "up" and not self._is_completion_active():
            if self._history:
                if self._history_idx == len(self._history):
                    self._draft = self.text
                if self._history_idx > 0:
                    self._history_idx -= 1
                    self.text = self._history[self._history_idx]
                    end_loc = self.get_cursor_line_end_location()
                    self.cursor_location = end_loc
            event.prevent_default()
            return

        if event.key == "down" and not self._is_completion_active():
            if self._history:
                if self._history_idx < len(self._history) - 1:
                    self._history_idx += 1
                    self.text = self._history[self._history_idx]
                else:
                    self._history_idx = len(self._history)
                    self.text = self._draft
                end_loc = self.get_cursor_line_end_location()
                self.cursor_location = end_loc
            event.prevent_default()
            return

        await super()._on_key(event)
        await self._check_completion_trigger()

    def _dismiss_if_invalid(self) -> None:
        popup = self._get_popup()
        if popup is None or not popup.popup_visible:
            return

        cursor_pos = self.cursor_location
        line_idx = cursor_pos[0]
        col = cursor_pos[1]

        if line_idx >= self.document.line_count:
            popup.hide()
            self._completion_mode = None
            return

        line = self.document.get_line(line_idx)
        before_cursor = line[:col]

        if self._completion_mode == "slash":
            if not before_cursor.startswith("/"):
                popup.hide()
                self._completion_mode = None
        elif self._completion_mode == "file":
            if "@" not in before_cursor:
                popup.hide()
                self._completion_mode = None

    async def _check_completion_trigger(self) -> None:
        popup = self._get_popup()
        if popup is None:
            return

        cursor_pos = self.cursor_location
        line_idx = cursor_pos[0]
        col = cursor_pos[1]

        if line_idx >= self.document.line_count:
            return
        line = self.document.get_line(line_idx)
        before_cursor = line[:col]

        if before_cursor.startswith("/") and " " not in before_cursor:
            self._completion_mode = "slash"
            popup.show_slash_commands(before_cursor)
            return

        at_pos = before_cursor.rfind("@")
        if at_pos >= 0:
            query = before_cursor[at_pos + 1 :]
            if " " not in query:
                self._completion_mode = "file"
                popup.show_file_matches(query)
                return

        if popup.popup_visible:
            popup.hide()
            self._completion_mode = None

    async def _insert_completion(self, text: str) -> None:
        cursor = self.cursor_location
        line_idx = cursor[0]
        col = cursor[1]

        if line_idx >= self.document.line_count:
            return
        line = self.document.get_line(line_idx)

        if self._completion_mode == "slash":
            self.document.replace_range((line_idx, 0), (line_idx, col), text + " ")
            self.cursor_location = (line_idx, len(text) + 1)
        elif self._completion_mode == "file":
            at_pos = line[:col].rfind("@")
            if at_pos >= 0:
                self.document.replace_range((line_idx, at_pos), (line_idx, col), text + " ")
                self.cursor_location = (line_idx, at_pos + len(text) + 1)

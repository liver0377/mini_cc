from __future__ import annotations

import asyncio
import time
from pathlib import Path

from textual.message import Message
from textual.widgets import OptionList

from mini_cc.tui.commands import match_commands

_MAX_FILE_RESULTS = 20
_CACHE_TTL = 5.0


def _scan_files(cwd: Path) -> list[str]:
    results: list[str] = []
    try:
        for p in cwd.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                rel = p.relative_to(cwd) if p.is_relative_to(cwd) else p
                results.append(str(rel))
    except (OSError, PermissionError):
        pass
    results.sort()
    return results


class CompletionPopup(OptionList):
    DEFAULT_CSS = """
    CompletionPopup {
        dock: bottom;
        height: auto;
        max-height: 14;
        min-height: 3;
        width: 1fr;
        background: #1c2128;
        border: tall #30363d;
        padding: 0 1;
        margin: 0 2;
        layer: overlay;
    }
    CompletionPopup:focus {
        border: tall #58a6ff;
    }
    """

    class Selected(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self) -> None:
        super().__init__(id="completion-popup")
        self._items: list[str] = []
        self._insert_values: list[str] = []
        self._popup_visible = False
        self._file_cache: list[str] = []
        self._file_cache_time: float = 0.0
        self._file_cache_cwd: str = ""
        self._scan_task: asyncio.Task[None] | None = None
        self._last_query: str = ""
        self.display = False

    @property
    def popup_visible(self) -> bool:
        return self._popup_visible

    def show_slash_commands(self, prefix: str) -> None:
        commands = match_commands(prefix)
        self._items = [f"{cmd.name}    {cmd.description}" for cmd in commands]
        self._insert_values = [cmd.name for cmd in commands]
        self._popup_visible = bool(self._items)
        self._rebuild()

    def show_file_matches(self, query: str) -> None:
        self._last_query = query
        cwd = Path.cwd()
        cache = self._get_file_cache(cwd)
        if cache is not None:
            self._apply_file_filter(cache, query)
            return

        self._items = []
        self._insert_values = []
        self._popup_visible = False
        self._rebuild()
        self._start_scan(query, cwd)

    def _get_file_cache(self, cwd: Path) -> list[str] | None:
        cwd_str = str(cwd)
        if (
            self._file_cache_cwd == cwd_str
            and self._file_cache
            and (time.monotonic() - self._file_cache_time) < _CACHE_TTL
        ):
            return self._file_cache
        return None

    def _start_scan(self, query: str, cwd: Path) -> None:
        if self._scan_task is not None and not self._scan_task.done():
            return
        self._scan_task = asyncio.create_task(self._do_scan(query, cwd))

    async def _do_scan(self, query: str, cwd: Path) -> None:
        loop = asyncio.get_running_loop()
        files = await loop.run_in_executor(None, _scan_files, cwd)
        self._file_cache = files
        self._file_cache_time = time.monotonic()
        self._file_cache_cwd = str(cwd)
        if query == self._last_query and self._popup_visible is False:
            self._apply_file_filter(files, query)

    def _apply_file_filter(self, files: list[str], query: str) -> None:
        if query:
            q_lower = query.lower()
            filtered = [f for f in files if q_lower in f.lower()][:_MAX_FILE_RESULTS]
        else:
            filtered = files[:_MAX_FILE_RESULTS]

        self._items = filtered
        self._insert_values = filtered
        self._popup_visible = bool(filtered)
        self._rebuild()

    def hide(self) -> None:
        self._popup_visible = False
        self._items = []
        self._insert_values = []
        self.clear_options()
        self.display = False

    def show(self) -> None:
        self.display = True

    def _rebuild(self) -> None:
        self.clear_options()
        for item in self._items:
            self.add_option(item)
        if self._items:
            self.highlighted = 0
            self.show()
        else:
            self.hide()

    def select_current(self) -> str | None:
        idx = self.highlighted
        if idx is not None and 0 <= idx < len(self._insert_values):
            return self._insert_values[idx]
        return None

    def has_items(self) -> bool:
        return len(self._items) > 0

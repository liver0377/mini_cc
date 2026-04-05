from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from textual.message import Message
from textual.widgets import OptionList

from mini_cc.tui.commands import match_commands

_MAX_FILE_RESULTS = 20
_CACHE_TTL = 30.0
_DEBOUNCE_DELAY = 0.05


def _scan_files_git(cwd: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return _scan_files_fallback(cwd)
        files = [line for line in result.stdout.splitlines() if line]
        files.sort()
        return files
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return _scan_files_fallback(cwd)


def _scan_files_fallback(cwd: Path) -> list[str]:
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


def _fuzzy_match_files(files: list[str], query: str, limit: int = _MAX_FILE_RESULTS) -> list[str]:
    if not query:
        return files[:limit]

    q_lower = query.lower()
    segments = [s for s in q_lower.split("/") if s]

    scored: list[tuple[int, str]] = []
    for f in files:
        f_lower = f.lower()
        if q_lower in f_lower:
            idx = f_lower.index(q_lower)
            score = 1000 - idx
            scored.append((score, f))
            continue

        if _segment_match(f_lower, segments):
            last_idx = _last_segment_index(f_lower, segments)
            scored.append((500 - last_idx, f))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [f for _, f in scored[:limit]]


def _segment_match(path_lower: str, segments: list[str]) -> bool:
    pos = 0
    for seg in segments:
        idx = path_lower.find(seg, pos)
        if idx == -1:
            return False
        pos = idx + len(seg)
    return True


def _last_segment_index(path_lower: str, segments: list[str]) -> int:
    pos = 0
    for seg in segments:
        idx = path_lower.find(seg, pos)
        if idx == -1:
            return 9999
        pos = idx + len(seg)
    return pos


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
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_query: str = ""
        self._pending_mode: str | None = None
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
        self._pending_query = query
        self._pending_mode = "file"

        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()

        cwd = Path.cwd()
        cache = self._get_file_cache(cwd)
        if cache is not None:
            self._do_apply_file_filter(cache, query)
            return

        self._items = []
        self._insert_values = []
        self._popup_visible = False
        self._rebuild()
        self._start_scan(query, cwd)

    def _do_apply_file_filter(self, files: list[str], query: str) -> None:
        filtered = _fuzzy_match_files(files, query)
        self._items = filtered
        self._insert_values = filtered
        self._popup_visible = bool(filtered)
        self._rebuild()

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
        files = await loop.run_in_executor(None, _scan_files_git, cwd)
        self._file_cache = files
        self._file_cache_time = time.monotonic()
        self._file_cache_cwd = str(cwd)
        if query == self._pending_query and not self._popup_visible:
            self._do_apply_file_filter(files, query)

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

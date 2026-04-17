from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    user_bg: str = "#1a1a2e"
    user_border: str = "#2d2d5e"
    user_accent: str = "#7ee787"
    assistant_accent: str = "#7c3aed"
    assistant_label: str = "#a78bfa"
    tool_bg: str = "#0d1117"
    tool_border: str = "#21262d"
    tool_success: str = "#238636"
    tool_fail: str = "#da3633"
    tool_name: str = "#58a6ff"
    system_muted: str = "#484f58"
    mode_plan: str = "#d29922"
    mode_build: str = "#238636"
    input_border: str = "#30363d"
    input_focus: str = "#58a6ff"
    input_bg: str = "#0d1117"
    spinner: str = "#58a6ff"
    separator: str = "#21262d"
    status_bg: str = "#161b22"
    status_separator: str = "#30363d"
    completion_bg: str = "#1c2128"
    completion_selected: str = "#1f6feb"
    completion_border: str = "#30363d"
    agent_colors: tuple[str, ...] = ("#58a6ff", "#bc8cff", "#d2a8ff", "#7ee787", "#79c0ff", "#ff7b72")


DEFAULT_THEME = Theme()

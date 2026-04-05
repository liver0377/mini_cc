from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    usage: str


BUILTIN_COMMANDS: list[SlashCommand] = [
    SlashCommand("/help", "显示帮助信息", "/help"),
    SlashCommand("/compact", "压缩对话上下文", "/compact"),
    SlashCommand("/clear", "清空聊天记录", "/clear"),
    SlashCommand("/mode", "切换 Plan/Build 模式", "/mode"),
    SlashCommand("/agents", "管理子 Agent", "/agents"),
    SlashCommand("/exit", "退出程序", "/exit"),
]


def match_commands(prefix: str) -> list[SlashCommand]:
    if not prefix.startswith("/"):
        return []
    return [cmd for cmd in BUILTIN_COMMANDS if cmd.name.startswith(prefix)]

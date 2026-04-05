from __future__ import annotations

import asyncio
import os
import threading

from rich import print as rprint
from rich.console import Console
from rich.text import Text

from mini_cc.context.engine_context import _EngineConfig
from mini_cc.models import (
    AgentCompletionEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    CompactOccurred,
    Event,
    QueryState,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.query_engine.engine import QueryEngine

_MAX_TOOL_OUTPUT_DISPLAY = 200


class REPLConfig(_EngineConfig):
    @classmethod
    def from_env(cls) -> REPLConfig:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        model = os.environ.get("OPENAI_MODEL") or None
        return cls(api_key=api_key, base_url=base_url, model=model)


def render_event(event: Event, *, console: Console | None = None) -> None:
    _print = rprint if console is None else console.print

    if isinstance(event, TextDelta):
        _print(event.content, end="")

    elif isinstance(event, ToolCallStart):
        _print(Text.from_markup(f"  [dim]⚙[/] [bold cyan]{event.name}[/][dim](...)[/]"))

    elif isinstance(event, ToolResultEvent):
        marker = "[bold green]✓[/]" if event.success else "[bold red]✗[/]"
        output_preview = event.output
        if len(output_preview) > _MAX_TOOL_OUTPUT_DISPLAY:
            output_preview = output_preview[:_MAX_TOOL_OUTPUT_DISPLAY] + "..."
        _print(Text.from_markup(f"  {marker} [cyan]{event.name}[/]: {output_preview}"))

    elif isinstance(event, AgentStartEvent):
        _print(
            Text.from_markup(f"  🤖 [bold magenta]子 Agent {event.agent_id}[/] [dim](Task #{event.task_id})[/] 启动")
        )
        if event.prompt:
            _print(Text.from_markup(f"    [dim]{event.prompt}[/]"))

    elif isinstance(event, AgentToolCallEvent):
        _print(
            Text.from_markup(
                f"    ⚙ [magenta]{event.agent_id}[/][dim] ▸ [/][bold cyan]{event.tool_name}[/][dim](...)[/]"
            )
        )

    elif isinstance(event, AgentToolResultEvent):
        marker = "[bold green]✓[/]" if event.success else "[bold red]✗[/]"
        preview = event.output_preview[:80] + ("..." if len(event.output_preview) > 80 else "")
        _print(
            Text.from_markup(
                f"    {marker} [magenta]{event.agent_id}[/][dim] ▸ [/][cyan]{event.tool_name}[/]: {preview}"
            )
        )

    elif isinstance(event, AgentCompletionEvent):
        status_marker = "[bold green]✓[/]" if event.success else "[bold red]✗[/]"
        _print(
            Text.from_markup(
                f"  {status_marker} [magenta]子 Agent {event.agent_id}[/]"
                f" [dim](Task #{event.task_id})[/]"
                f" {'完成' if event.success else '失败'}"
            )
        )
        if event.output:
            preview = event.output[:80] + ("..." if len(event.output) > 80 else "")
            _print(Text.from_markup(f"    [dim]{preview}[/]"))

    elif isinstance(event, CompactOccurred):
        label = {
            "auto": "上下文已自动压缩",
            "reactive": "上下文超出限制，已自动压缩后重试",
        }.get(event.reason, "对话已压缩")
        _print(Text.from_markup(f"  [dim]（{label}）[/]"))


async def _collect_events(
    engine: QueryEngine,
    prompt: str,
    state: QueryState,
    interrupted_event: threading.Event,
) -> list[Event]:
    events: list[Event] = []
    try:
        async for event in engine.submit_message(prompt, state):
            events.append(event)
            render_event(event)
    except KeyboardInterrupt:
        interrupted_event.set()
    return events


def run_message(
    engine: QueryEngine,
    prompt: str,
    state: QueryState,
    interrupted_event: threading.Event,
) -> list[Event]:
    interrupted_event.clear()
    try:
        events = asyncio.run(_collect_events(engine, prompt, state, interrupted_event))
    except KeyboardInterrupt:
        interrupted_event.set()
        rprint("\n[dim]（已中断）[/]")
        return []
    return events

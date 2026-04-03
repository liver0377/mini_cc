from __future__ import annotations

import asyncio
import os
import sys
import threading

from rich import print as rprint
from rich.console import Console
from rich.text import Text

from mini_cc.context.system_prompt import EnvInfo, SystemPromptBuilder, collect_env_info
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.query_engine.engine import QueryEngine
from mini_cc.query_engine.state import (
    AgentCompletionNotificationEvent,
    Event,
    QueryState,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.tool_executor.executor import StreamingToolExecutor
from mini_cc.tools import create_default_registry

_MAX_TOOL_OUTPUT_DISPLAY = 200


class REPLConfig:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://api.openai.com/v1"
        self.model = model or "gpt-4o"

    @classmethod
    def from_env(cls) -> REPLConfig:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        model = os.environ.get("OPENAI_MODEL") or None
        return cls(api_key=api_key, base_url=base_url, model=model)


def load_dotenv() -> None:
    try:
        from dotenv import load_dotenv as _load

        _load()
    except ImportError:
        pass


class EngineContext:
    def __init__(self, engine: QueryEngine, prompt_builder: SystemPromptBuilder, env_info: EnvInfo) -> None:
        self.engine = engine
        self.prompt_builder = prompt_builder
        self.env_info = env_info


def create_engine(
    config: REPLConfig | None = None,
    *,
    interrupted_event: threading.Event | None = None,
) -> EngineContext:
    if config is None:
        load_dotenv()
        config = REPLConfig.from_env()

    if not config.api_key:
        rprint("[bold red]错误:[/] 未设置 OPENAI_API_KEY 环境变量")
        rprint("[dim]请在 .env 文件或环境变量中设置 OPENAI_API_KEY[/]")
        sys.exit(1)

    from mini_cc.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
    )

    registry = create_default_registry()
    executor = StreamingToolExecutor(registry)

    interrupt_flag = interrupted_event or threading.Event()

    tool_use_ctx = ToolUseContext(
        get_schemas=registry.to_api_format,
        execute=executor.run,
        is_interrupted=interrupt_flag.is_set,
    )

    engine = QueryEngine(stream_fn=provider.stream, tool_use_ctx=tool_use_ctx)
    env_info = collect_env_info(config.model)
    prompt_builder = SystemPromptBuilder()

    return EngineContext(engine=engine, prompt_builder=prompt_builder, env_info=env_info)


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

    elif isinstance(event, AgentCompletionNotificationEvent):
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

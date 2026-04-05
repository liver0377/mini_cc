from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich import print as rprint
from rich.panel import Panel
from rich.text import Text

from mini_cc import __version__
from mini_cc.compression.compressor import compress_messages, replace_with_summary
from mini_cc.context.engine_context import EngineContext, create_engine
from mini_cc.models import Message, QueryState, Role
from mini_cc.repl import run_message

app = typer.Typer(
    name="mini-cc",
    help="Mini Claude Code — 轻量级多 Agent 协作代码助手 CLI",
    add_completion=False,
)

PLAN_MODE = "plan"
BUILD_MODE = "build"


def _version_callback(value: bool) -> None:
    if value:
        rprint(f"mini-cc {__version__}")
        raise typer.Exit


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", "-v", help="显示版本号", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui)


class ModeState:
    def __init__(self, mode: str = BUILD_MODE) -> None:
        self.mode = mode

    def toggle(self) -> str:
        self.mode = PLAN_MODE if self.mode == BUILD_MODE else BUILD_MODE
        return self.mode


def _get_prompt_message(mode_state: ModeState) -> Callable[[], str]:
    def _message() -> str:
        mode_indicator = f"[{mode_state.mode}] " if mode_state.mode == PLAN_MODE else ""
        return f"{mode_indicator}> "

    return _message


_DATA_DIR = Path.home() / ".local" / "share" / "mini_cc"


def _create_session(mode_state: ModeState) -> PromptSession[str]:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    kb = KeyBindings()

    @kb.add("tab")
    def _toggle_mode(event: object) -> None:
        mode_state.toggle()
        if mode_state.mode == PLAN_MODE:
            sys.stdout.write("\r\n\033[33m切换到 Plan (只读) 模式\033[0m\r\n")
        else:
            sys.stdout.write("\r\n\033[32m切换到 Build (读写) 模式\033[0m\r\n")
        sys.stdout.flush()
        prompt_app = event.app  # type: ignore[attr-defined]
        prompt_app.invalidate()

    return PromptSession[str](
        history=FileHistory(str(_DATA_DIR / "history")),
        multiline=False,
        enable_open_in_editor=True,
        key_bindings=kb,
    )


def _print_banner(mode: str) -> None:
    if mode == BUILD_MODE:
        mode_text = "\n当前模式: [bold green]Build[/] (读写)"
    else:
        mode_text = "\n当前模式: [bold yellow]Plan[/] (只读)"
    rprint(
        Panel(
            Text.from_markup(
                f"Mini Claude Code [dim]v{__version__}[/]\n"
                "输入消息开始对话，输入 [bold]exit[/] 或 [bold]quit[/] 退出\n"
                "按 [bold]Tab[/] 切换 Plan/Build 模式\n"
                "[dim]按 Ctrl+C 中断输入，Ctrl+D 退出[/]"
                f"{mode_text}"
            ),
            title="mini-cc",
            border_style="green",
            padding=(1, 2),
        )
    )


def _build_initial_state(ctx: EngineContext, mode: str) -> QueryState:
    system_content = ctx.prompt_builder.build(ctx.env_info, mode=mode)
    return QueryState(messages=[Message(role=Role.SYSTEM, content=system_content)])


def _rebuild_system_message(state: QueryState, ctx: EngineContext, mode: str) -> None:
    system_content = ctx.prompt_builder.build(ctx.env_info, mode=mode)
    if state.messages and state.messages[0].role == Role.SYSTEM:
        state.messages[0] = Message(role=Role.SYSTEM, content=system_content)
    else:
        state.messages.insert(0, Message(role=Role.SYSTEM, content=system_content))


def _print_mode_change(mode: str) -> None:
    if mode == BUILD_MODE:
        rprint("[dim]切换到 [bold green]Build[/] (读写) 模式[/]")
    else:
        rprint("[dim]切换到 [bold yellow]Plan[/] (只读) 模式[/]")


def _get_current_system_mode(state: QueryState) -> str:
    if state.messages and state.messages[0].role == Role.SYSTEM:
        content = state.messages[0].content or ""
        if "Mode: plan" in content:
            return PLAN_MODE
    return BUILD_MODE


@app.command()
def tui() -> None:
    """启动 Textual TUI 界面（默认）。"""
    from mini_cc.tui import MiniCCApp

    engine_ctx = create_engine()
    tui_app = MiniCCApp(engine_ctx)
    tui_app.run()


@app.command()
def chat() -> None:
    """启动 prompt_toolkit 交互式对话（备用）。"""
    mode_state = ModeState(BUILD_MODE)
    interrupted_event = threading.Event()
    engine_ctx = create_engine(interrupted_event=interrupted_event)

    session = _create_session(mode_state)
    state = _build_initial_state(engine_ctx, mode_state.mode)
    _print_banner(mode_state.mode)

    while True:
        try:
            user_input = session.prompt(_get_prompt_message(mode_state))
        except KeyboardInterrupt:
            rprint("[dim]（输入已中断）[/]")
            continue
        except EOFError:
            rprint("[dim]再见！[/]")
            break

        if mode_state.mode != _get_current_system_mode(state):
            _rebuild_system_message(state, engine_ctx, mode_state.mode)
            _print_mode_change(mode_state.mode)

        text = user_input.strip()
        if not text:
            continue

        if text.lower() in {"exit", "quit"}:
            rprint("[dim]再见！[/]")
            break

        if text.lower() == "/compact":
            try:
                summary = asyncio.run(compress_messages(state.messages, engine_ctx.engine._stream_fn, engine_ctx.model))
                replace_with_summary(state, summary)
                rprint("[dim]（对话已手动压缩）[/]")
            except Exception as e:
                rprint(f"[bold red]压缩失败: {e}[/]")
            rprint()
            continue

        run_message(engine_ctx.engine, text, state, interrupted_event)

        rprint()


if __name__ == "__main__":
    app()

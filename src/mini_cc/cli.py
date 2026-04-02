from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich import print as rprint
from rich.panel import Panel
from rich.text import Text

from mini_cc import __version__
from mini_cc.query_engine.state import Message, QueryState, Role
from mini_cc.repl import EngineContext, create_engine, run_message

app = typer.Typer(
    name="mini-cc",
    help="Mini Claude Code — 轻量级多 Agent 协作代码助手 CLI",
    no_args_is_help=True,
    add_completion=False,
)

PLAN_MODE = "plan"
BUILD_MODE = "build"


def _version_callback(value: bool) -> None:
    if value:
        rprint(f"mini-cc {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", "-v", help="显示版本号", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    pass


def _get_prompt_message(mode: str) -> Callable[[], str]:
    def _message() -> str:
        mode_indicator = f"[{mode}] " if mode == PLAN_MODE else ""
        return f"{mode_indicator}> "

    return _message


_DATA_DIR = Path.home() / ".local" / "share" / "mini_cc"


def _create_session() -> PromptSession[str]:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return PromptSession[str](
        history=FileHistory(str(_DATA_DIR / "history")),
        multiline=False,
        enable_open_in_editor=True,
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


@app.command()
def chat() -> None:
    """启动交互式对话循环。"""
    mode = BUILD_MODE
    interrupted_event = threading.Event()
    engine_ctx = create_engine(interrupted_event=interrupted_event)

    session = _create_session()
    state = _build_initial_state(engine_ctx, mode)
    _print_banner(mode)

    while True:
        try:
            user_input = session.prompt(_get_prompt_message(mode))
        except KeyboardInterrupt:
            rprint("[dim]（输入已中断）[/]")
            continue
        except EOFError:
            rprint("[dim]再见！[/]")
            break

        text = user_input.strip()
        if not text:
            continue

        if text.lower() in {"exit", "quit"}:
            rprint("[dim]再见！[/]")
            break

        # Tab toggle is handled by prompt_toolkit bindings; fallback: explicit commands
        if text in {"/plan", "/build"}:
            mode = PLAN_MODE if text == "/plan" else BUILD_MODE
            _rebuild_system_message(state, engine_ctx, mode)
            mode_label = "Plan (只读)" if mode == PLAN_MODE else "Build (读写)"
            rprint(f"[dim]切换到 {mode_label} 模式[/]")
            continue

        run_message(engine_ctx.engine, text, state, interrupted_event)

        rprint()


if __name__ == "__main__":
    app()

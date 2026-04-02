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
from mini_cc.query_engine.state import QueryState
from mini_cc.repl import create_engine, run_message

app = typer.Typer(
    name="mini-cc",
    help="Mini Claude Code — 轻量级多 Agent 协作代码助手 CLI",
    no_args_is_help=True,
    add_completion=False,
)


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


def _get_prompt_message() -> Callable[[], str]:
    def _message() -> str:
        return "> "

    return _message


_DATA_DIR = Path.home() / ".local" / "share" / "mini_cc"


def _create_session() -> PromptSession[str]:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return PromptSession[str](
        history=FileHistory(str(_DATA_DIR / "history")),
        multiline=False,
        enable_open_in_editor=True,
    )


def _print_banner() -> None:
    rprint(
        Panel(
            Text.from_markup(
                f"Mini Claude Code [dim]v{__version__}[/]\n"
                "输入消息开始对话，输入 [bold]exit[/] 或 [bold]quit[/] 退出\n"
                "[dim]按 Ctrl+C 中断输入，Ctrl+D 退出[/]"
            ),
            title="mini-cc",
            border_style="green",
            padding=(1, 2),
        )
    )


@app.command()
def chat() -> None:
    """启动交互式对话循环。"""
    _print_banner()

    interrupted_event = threading.Event()
    engine = create_engine(interrupted_event=interrupted_event)

    session = _create_session()
    state = QueryState()

    while True:
        try:
            user_input = session.prompt(_get_prompt_message())
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

        rprint(Panel(text, title="You", border_style="blue"))

        run_message(engine, text, state, interrupted_event)

        rprint()


if __name__ == "__main__":
    app()

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich import print as rprint


class ProviderFactory:
    @staticmethod
    def create(config: _EngineConfig) -> Any:
        from mini_cc.providers.openai import OpenAIProvider

        return OpenAIProvider(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
        )


class ToolingFactory:
    @staticmethod
    def create_default(
        *,
        is_interrupted: Callable[[], bool] | None = None,
    ) -> Any:
        from mini_cc.runtime.execution.executor import StreamingToolExecutor
        from mini_cc.tools import create_default_registry

        registry = create_default_registry()
        return registry, StreamingToolExecutor(registry, is_interrupted=is_interrupted)

    @staticmethod
    def create_readonly(
        *,
        is_interrupted: Callable[[], bool] | None = None,
    ) -> Any:
        from mini_cc.runtime.execution.executor import StreamingToolExecutor
        from mini_cc.tools import create_readonly_registry

        registry = create_readonly_registry()
        return registry, StreamingToolExecutor(registry, is_interrupted=is_interrupted)


class _EngineConfig:
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
    def from_env(cls) -> _EngineConfig:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        model = os.environ.get("OPENAI_MODEL") or None
        return cls(api_key=api_key, base_url=base_url, model=model)


def load_dotenv() -> None:
    try:
        from dotenv import load_dotenv as _load

        _load(dotenv_path=_project_root() / ".env", override=True)
    except ImportError:
        pass


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _validate_config(config: _EngineConfig) -> None:
    if not config.api_key:
        rprint("[bold red]错误:[/] 未设置 OPENAI_API_KEY 环境变量")
        rprint("[dim]请在 .env 文件或环境变量中设置 OPENAI_API_KEY[/]")
        sys.exit(1)

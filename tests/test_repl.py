from __future__ import annotations

import os
from io import StringIO
from unittest.mock import patch

from rich.console import Console

from mini_cc.query_engine.state import (
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.repl import REPLConfig, render_event


class TestREPLConfig:
    def test_from_env_with_values(self) -> None:
        env = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://example.com/v1",
            "OPENAI_MODEL": "gpt-4",
        }
        with patch.dict(os.environ, env, clear=False):
            config = REPLConfig.from_env()
        assert config.api_key == "test-key"
        assert config.base_url == "https://example.com/v1"
        assert config.model == "gpt-4"

    def test_from_env_defaults(self) -> None:
        config = REPLConfig(api_key="test-key")
        assert config.base_url == "https://api.openai.com/v1"
        assert config.model == "gpt-4o"

    def test_from_env_missing_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            config = REPLConfig.from_env()
        assert config.api_key == ""


class TestRenderEvent:
    def _make_console(self) -> tuple[Console, StringIO]:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        return console, buf

    def test_text_delta(self) -> None:
        console, buf = self._make_console()
        render_event(TextDelta(content="Hello"), console=console)
        assert "Hello" in buf.getvalue()

    def test_tool_call_start(self) -> None:
        console, buf = self._make_console()
        render_event(ToolCallStart(tool_call_id="tc_1", name="file_read"), console=console)
        output = buf.getvalue()
        assert "file_read" in output

    def test_tool_result_success(self) -> None:
        console, buf = self._make_console()
        render_event(
            ToolResultEvent(tool_call_id="tc_1", name="bash", output="ok", success=True),
            console=console,
        )
        output = buf.getvalue()
        assert "bash" in output
        assert "ok" in output

    def test_tool_result_failure(self) -> None:
        console, buf = self._make_console()
        render_event(
            ToolResultEvent(
                tool_call_id="tc_1",
                name="bash",
                output="Permission denied",
                success=False,
            ),
            console=console,
        )
        output = buf.getvalue()
        assert "bash" in output
        assert "Permission denied" in output

    def test_tool_result_truncation(self) -> None:
        console, buf = self._make_console()
        long_output = "x" * 500
        render_event(
            ToolResultEvent(tool_call_id="tc_1", name="grep", output=long_output, success=True),
            console=console,
        )
        output = buf.getvalue()
        assert "..." in output
        assert len(output) < 500

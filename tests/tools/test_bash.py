from __future__ import annotations

from mini_cc.tools.bash import Bash


class TestBash:
    def test_simple_command(self) -> None:
        tool = Bash()
        result = tool.execute(command="echo hello")

        assert result.success is True
        assert "hello" in result.output

    def test_command_with_stderr(self) -> None:
        tool = Bash()
        result = tool.execute(command="echo err >&2")

        assert result.success is True
        assert "err" in result.output

    def test_nonzero_exit_code(self) -> None:
        tool = Bash()
        result = tool.execute(command="exit 1")

        assert result.success is False
        assert "退出码" in result.error

    def test_command_not_found(self) -> None:
        tool = Bash()
        result = tool.execute(command="nonexistent_command_xyz")

        assert result.success is False

    def test_timeout(self) -> None:
        tool = Bash()
        result = tool.execute(command="sleep 10", timeout=100)

        assert result.success is False
        assert "超时" in result.error

    def test_multiword_output(self) -> None:
        tool = Bash()
        result = tool.execute(command="echo -n 'hello world'")

        assert result.success is True
        assert result.output == "hello world"

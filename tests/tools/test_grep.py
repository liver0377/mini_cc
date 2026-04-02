from __future__ import annotations

from pathlib import Path

from mini_cc.tools.grep import GrepTool


class TestGrepTool:
    def test_find_matching_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    print('hello')\n\ndef world():\n    pass\n", encoding="utf-8")

        tool = GrepTool()
        result = tool.execute(pattern="def", path=str(tmp_path))

        assert result.success is True
        assert "def hello():" in result.output
        assert "def world():" in result.output

    def test_no_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("hello world", encoding="utf-8")

        tool = GrepTool()
        result = tool.execute(pattern="nonexistent_pattern", path=str(tmp_path))

        assert result.success is True
        assert "未找到" in result.output

    def test_include_filter(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("import os\n", encoding="utf-8")

        tool = GrepTool()
        result = tool.execute(pattern="import", include="*.py", path=str(tmp_path))

        assert result.success is True
        assert "a.py" in result.output
        assert "b.txt" not in result.output

    def test_invalid_regex(self, tmp_path: Path) -> None:
        tool = GrepTool()
        result = tool.execute(pattern="(?P<invalid", path=str(tmp_path))

        assert result.success is False

    def test_nonexistent_path(self, tmp_path: Path) -> None:
        tool = GrepTool()
        result = tool.execute(pattern="test", path=str(tmp_path / "nope"))

        assert result.success is False

    def test_search_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")

        tool = GrepTool()
        result = tool.execute(pattern="line2", path=str(f))

        assert result.success is True
        assert "line2" in result.output
        assert "line1" not in result.output

    def test_line_numbers_in_output(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("aaa\nbbb\naaa\n", encoding="utf-8")

        tool = GrepTool()
        result = tool.execute(pattern="aaa", path=str(f))

        assert result.success is True
        lines = result.output.strip().split("\n")
        assert len(lines) == 2
        assert ":1:" in lines[0]
        assert ":3:" in lines[1]

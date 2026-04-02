from __future__ import annotations

from pathlib import Path

from mini_cc.tools.file_edit import FileEdit


class TestFileEdit:
    def test_replace_single_match(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")

        tool = FileEdit()
        result = tool.execute(file_path=str(f), old_string="world", new_string="python")

        assert result.success is True
        assert result.output == "文件编辑成功"
        assert f.read_text(encoding="utf-8") == "hello python"

    def test_no_match(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")

        tool = FileEdit()
        result = tool.execute(file_path=str(f), old_string="xxx", new_string="yyy")

        assert result.success is False
        assert "未找到匹配" in result.error
        assert f.read_text(encoding="utf-8") == "hello world"

    def test_multiple_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("aaa bbb aaa", encoding="utf-8")

        tool = FileEdit()
        result = tool.execute(file_path=str(f), old_string="aaa", new_string="ccc")

        assert result.success is False
        assert "2 处匹配" in result.error

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        tool = FileEdit()
        result = tool.execute(file_path=str(tmp_path / "nope.txt"), old_string="a", new_string="b")

        assert result.success is False
        assert "文件不存在" in result.error

    def test_replace_multiline(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3", encoding="utf-8")

        tool = FileEdit()
        result = tool.execute(file_path=str(f), old_string="line1\nline2", new_string="replaced")

        assert result.success is True
        assert f.read_text(encoding="utf-8") == "replaced\nline3"

    def test_replace_with_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")

        tool = FileEdit()
        result = tool.execute(file_path=str(f), old_string=" world", new_string="")

        assert result.success is True
        assert f.read_text(encoding="utf-8") == "hello"

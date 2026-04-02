from __future__ import annotations

from pathlib import Path

from mini_cc.tools.file_write import FileWrite


class TestFileWrite:
    def test_write_new_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"

        tool = FileWrite()
        result = tool.execute(file_path=str(f), content="hello world")

        assert result.success is True
        assert result.output == "文件写入成功"
        assert f.read_text(encoding="utf-8") == "hello world"

    def test_overwrite_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("old content", encoding="utf-8")

        tool = FileWrite()
        result = tool.execute(file_path=str(f), content="new content")

        assert result.success is True
        assert f.read_text(encoding="utf-8") == "new content"

    def test_create_parent_directories(self, tmp_path: Path) -> None:
        f = tmp_path / "a" / "b" / "c" / "test.txt"

        tool = FileWrite()
        result = tool.execute(file_path=str(f), content="deep nested")

        assert result.success is True
        assert f.read_text(encoding="utf-8") == "deep nested"

    def test_write_empty_content(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"

        tool = FileWrite()
        result = tool.execute(file_path=str(f), content="")

        assert result.success is True
        assert f.read_text(encoding="utf-8") == ""

    def test_write_unicode_content(self, tmp_path: Path) -> None:
        f = tmp_path / "unicode.txt"

        tool = FileWrite()
        result = tool.execute(file_path=str(f), content="你好世界 🌍")

        assert result.success is True
        assert f.read_text(encoding="utf-8") == "你好世界 🌍"

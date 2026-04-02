from __future__ import annotations

from pathlib import Path

from mini_cc.tools.file_read import FileRead


class TestFileRead:
    def test_read_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("hello world", encoding="utf-8")

        tool = FileRead()
        result = tool.execute(file_path=str(f))

        assert result.success is True
        assert result.output == "hello world"

    def test_read_nonexistent_file(self, tmp_path: Path) -> None:
        tool = FileRead()
        result = tool.execute(file_path=str(tmp_path / "nope.txt"))

        assert result.success is False
        assert "文件不存在" in result.error

    def test_read_directory(self, tmp_path: Path) -> None:
        tool = FileRead()
        result = tool.execute(file_path=str(tmp_path))

        assert result.success is False
        assert "路径不是文件" in result.error

    def test_read_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")

        tool = FileRead()
        result = tool.execute(file_path=str(f))

        assert result.success is True
        assert result.output == ""

    def test_read_binary_file(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x80\x81\x82\x83")

        tool = FileRead()
        result = tool.execute(file_path=str(f))

        assert result.success is False
        assert "编码不支持" in result.error

    def test_read_multiline_file(self, tmp_path: Path) -> None:
        f = tmp_path / "multi.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")

        tool = FileRead()
        result = tool.execute(file_path=str(f))

        assert result.success is True
        assert result.output == "line1\nline2\nline3\n"

from __future__ import annotations

from pathlib import Path

from mini_cc.tools.glob import GlobTool


class TestGlobTool:
    def test_find_matching_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "b.py").write_text("", encoding="utf-8")
        (tmp_path / "c.txt").write_text("", encoding="utf-8")

        tool = GlobTool()
        result = tool.execute(pattern="*.py", path=str(tmp_path))

        assert result.success is True
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output

    def test_no_matches(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("", encoding="utf-8")

        tool = GlobTool()
        result = tool.execute(pattern="*.py", path=str(tmp_path))

        assert result.success is True
        assert "未找到" in result.output

    def test_nonexistent_path(self, tmp_path: Path) -> None:
        tool = GlobTool()
        result = tool.execute(pattern="*.py", path=str(tmp_path / "nope"))

        assert result.success is False

    def test_recursive_pattern(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "a.py").write_text("", encoding="utf-8")
        (sub / "b.py").write_text("", encoding="utf-8")

        tool = GlobTool()
        result = tool.execute(pattern="**/*.py", path=str(tmp_path))

        assert result.success is True
        assert "a.py" in result.output
        assert "b.py" in result.output

    def test_default_path_is_cwd(self, tmp_path: Path) -> None:
        tool = GlobTool()
        result = tool.execute(pattern="*.toml")

        assert result.success is True

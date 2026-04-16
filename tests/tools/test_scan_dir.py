from __future__ import annotations

from pathlib import Path

from mini_cc.tools.scan_dir import ScanDirTool


class TestScanDirTool:
    def test_scan_dir_summarizes_nested_structure(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "core").mkdir()
        (tmp_path / "src" / "core" / "engine.py").write_text("", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_engine.py").write_text("", encoding="utf-8")

        tool = ScanDirTool()
        result = tool.execute(path=str(tmp_path), max_depth=2, max_entries=20)

        assert result.success is True
        assert "root:" in result.output
        assert "src/" in result.output
        assert "tests/" in result.output
        assert "summary:" in result.output

    def test_scan_dir_rejects_missing_path(self, tmp_path: Path) -> None:
        tool = ScanDirTool()
        result = tool.execute(path=str(tmp_path / "missing"))

        assert result.success is False
        assert "路径不存在" in result.error

    def test_scan_dir_can_hide_hidden_entries(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()

        tool = ScanDirTool()
        result = tool.execute(path=str(tmp_path), include_hidden=False)

        assert result.success is True
        assert ".hidden" not in result.output
        assert "visible/" in result.output

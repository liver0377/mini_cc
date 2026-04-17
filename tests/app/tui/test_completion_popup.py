from __future__ import annotations

from pathlib import Path

from mini_cc.app.tui.widgets.completion_popup import (
    _fuzzy_match_files,
    _scan_files_fallback,
    _segment_match,
)


class TestFuzzyMatchFiles:
    def test_empty_query_returns_first_n(self):
        files = ["a.py", "b.py", "c.py"]
        result = _fuzzy_match_files(files, "")
        assert result == files

    def test_exact_substring_match(self):
        files = ["src/main.py", "src/utils.py", "tests/test_main.py"]
        result = _fuzzy_match_files(files, "main.py")
        assert result == ["src/main.py", "tests/test_main.py"]

    def test_case_insensitive(self):
        files = ["src/Main.py", "src/utils.py"]
        result = _fuzzy_match_files(files, "main")
        assert "src/Main.py" in result

    def test_segment_match(self):
        files = ["src/mini_cc/tui/app.py", "src/other/thing.py"]
        result = _fuzzy_match_files(files, "tui/app")
        assert "src/mini_cc/tui/app.py" in result

    def test_limit(self):
        files = [f"file_{i}.py" for i in range(100)]
        result = _fuzzy_match_files(files, "file", limit=5)
        assert len(result) == 5


class TestSegmentMatch:
    def test_single_segment(self):
        assert _segment_match("src/main.py", ["main"])

    def test_multiple_segments(self):
        assert _segment_match("src/mini_cc/tui/app.py", ["tui", "app"])

    def test_order_matters(self):
        assert not _segment_match("src/mini_cc/tui/app.py", ["app", "tui"])

    def test_no_match(self):
        assert not _segment_match("src/main.py", ["xyz"])


class TestScanFilesFallback:
    def test_fallback_respects_git_exclusion(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("dummy")
        (tmp_path / "hello.py").write_text("print('hi')")

        result = _scan_files_fallback(tmp_path)
        assert "hello.py" in result
        assert not any(".git" in f for f in result)

    def test_fallback_returns_sorted(self, tmp_path: Path):
        (tmp_path / "z.py").write_text("")
        (tmp_path / "a.py").write_text("")
        (tmp_path / "m.py").write_text("")

        result = _scan_files_fallback(tmp_path)
        assert result == sorted(result)

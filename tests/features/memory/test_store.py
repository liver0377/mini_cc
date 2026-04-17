from __future__ import annotations

from pathlib import Path

from mini_cc.features.memory.store import (
    MemoryMeta,
    _sanitize_filename,
    get_memory_dir,
    list_memories,
    load_memory_index,
    project_id,
    save_memory,
)


class TestProjectId:
    def test_deterministic(self, tmp_path: Path) -> None:
        cwd = tmp_path / "my_project"
        assert project_id(cwd) == project_id(cwd)

    def test_different_paths_different_ids(self, tmp_path: Path) -> None:
        a = tmp_path / "project_a"
        b = tmp_path / "project_b"
        assert project_id(a) != project_id(b)

    def test_length(self, tmp_path: Path) -> None:
        result = project_id(tmp_path / "x")
        assert len(result) == 12

    def test_hex_characters(self, tmp_path: Path) -> None:
        result = project_id(tmp_path / "x")
        assert all(c in "0123456789abcdef" for c in result)


class TestSanitizeFilename:
    def test_simple(self) -> None:
        assert _sanitize_filename("user_role") == "user_role"

    def test_chinese_replaced(self) -> None:
        result = _sanitize_filename("用户偏好")
        assert all(c.isalnum() or c == "_" for c in result)

    def test_spaces_replaced(self) -> None:
        assert _sanitize_filename("my memory") == "my_memory"

    def test_empty_returns_unnamed(self) -> None:
        assert _sanitize_filename("!!!") == "unnamed"

    def test_lowercase(self) -> None:
        assert _sanitize_filename("MyMemory") == "mymemory"


class TestGetMemoryDir:
    def test_path_format(self, tmp_path: Path) -> None:
        cwd = tmp_path / "my_app"
        result = get_memory_dir(cwd)
        assert result.name == "memory"
        assert "projects" in str(result)

    def test_does_not_create_dir(self, tmp_path: Path) -> None:
        cwd = tmp_path / "my_app"
        result = get_memory_dir(cwd)
        assert not result.exists()


class TestSaveAndList:
    def test_save_creates_file(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / ".mini_cc" / "projects"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)

        path = save_memory(tmp_path, "user_role", "user", "用户是数据科学家")
        assert path.exists()
        assert path.name == "user_role.md"

        content = path.read_text()
        assert "---" in content
        assert "name: user_role" in content
        assert "type: user" in content
        assert "用户是数据科学家" in content

    def test_list_returns_saved(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / ".mini_cc" / "projects"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)

        save_memory(tmp_path, "user_role", "user", "content", "用户画像")
        memories = list_memories(tmp_path)

        assert len(memories) == 1
        assert memories[0].name == "user_role"
        assert memories[0].type == "user"
        assert memories[0].description == "用户画像"

    def test_list_empty_when_no_dir(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / "nonexistent"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)
        assert list_memories(tmp_path) == []

    def test_invalid_type_raises(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / ".mini_cc" / "projects"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)

        import pytest

        with pytest.raises(ValueError, match="Invalid memory type"):
            save_memory(tmp_path, "test", "invalid", "content")

    def test_save_updates_index(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / ".mini_cc" / "projects"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)

        save_memory(tmp_path, "user_role", "user", "content", "用户角色")
        index = load_memory_index(tmp_path)

        assert "user_role" in index
        assert "用户角色" in index


class TestLoadMemoryIndex:
    def test_returns_empty_when_no_file(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / "nonexistent"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)
        assert load_memory_index(tmp_path) == ""

    def test_returns_content(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / ".mini_cc" / "projects"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)

        save_memory(tmp_path, "test", "project", "content", "desc")
        result = load_memory_index(tmp_path)
        assert "test" in result


class TestMultipleMemories:
    def test_save_multiple(self, tmp_path: Path, monkeypatch) -> None:
        base = tmp_path / ".mini_cc" / "projects"
        monkeypatch.setattr("mini_cc.features.memory.store._BASE_DIR", base)

        save_memory(tmp_path, "user_role", "user", "用户偏好")
        save_memory(tmp_path, "feedback_testing", "feedback", "不要删除测试")
        save_memory(tmp_path, "project_release", "project", "v2.0 计划")

        memories = list_memories(tmp_path)
        assert len(memories) == 3

        index = load_memory_index(tmp_path)
        assert "user_role" in index
        assert "feedback_testing" in index
        assert "project_release" in index

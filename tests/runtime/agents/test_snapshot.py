from __future__ import annotations

import json
from pathlib import Path

from mini_cc.runtime.agents import SnapshotService


def _make_project(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    if files:
        for name, content in files.items():
            p = project / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return project


class TestSnapshot:
    async def test_snapshot_modified_file(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"src/foo.py": "original"})
        svc = SnapshotService(project, "a1b2c3d4")

        svc.snapshot(str(project / "src" / "foo.py"))

        snapshots = svc.list_snapshots()
        assert "src/foo.py" in snapshots
        assert snapshots["src/foo.py"] == "modified"
        backup = project / ".mini_cc" / "snapshots" / "a1b2c3d4" / "src" / "foo.py"
        assert backup.read_text(encoding="utf-8") == "original"

    async def test_snapshot_created_file(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        svc = SnapshotService(project, "e5f6a7b8")

        svc.snapshot(str(project / "new_file.py"))

        snapshots = svc.list_snapshots()
        assert "new_file.py" in snapshots
        assert snapshots["new_file.py"] == "created"

    async def test_snapshot_idempotent(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"a.py": "v1"})
        svc = SnapshotService(project, "test1234")

        svc.snapshot(str(project / "a.py"))
        (project / "a.py").write_text("v2", encoding="utf-8")
        svc.snapshot(str(project / "a.py"))

        snapshots = svc.list_snapshots()
        assert len(snapshots) == 1
        backup = project / ".mini_cc" / "snapshots" / "test1234" / "a.py"
        assert backup.read_text(encoding="utf-8") == "v1"

    async def test_snapshot_outside_project_ignored(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        svc = SnapshotService(project, "test9999")

        svc.snapshot("/tmp/outside.py")

        assert len(svc.list_snapshots()) == 0


class TestRestoreAll:
    async def test_restore_modified(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"src/app.py": "original"})
        svc = SnapshotService(project, "r1e2s3t4")

        svc.snapshot(str(project / "src" / "app.py"))
        (project / "src" / "app.py").write_text("modified", encoding="utf-8")

        restored = svc.restore_all()

        assert "src/app.py" in restored
        assert (project / "src" / "app.py").read_text(encoding="utf-8") == "original"

    async def test_restore_created(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        svc = SnapshotService(project, "c1r2e3a4")

        svc.snapshot(str(project / "new.py"))
        (project / "new.py").write_text("created content", encoding="utf-8")

        restored = svc.restore_all()

        assert "new.py" in restored
        assert not (project / "new.py").exists()

    async def test_restore_multiple(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"a.py": "old_a", "b.py": "old_b"})
        svc = SnapshotService(project, "m1u2l3t4")

        svc.snapshot(str(project / "a.py"))
        svc.snapshot(str(project / "b.py"))
        svc.snapshot(str(project / "c.py"))
        (project / "a.py").write_text("new_a", encoding="utf-8")
        (project / "b.py").write_text("new_b", encoding="utf-8")
        (project / "c.py").write_text("new_c", encoding="utf-8")

        restored = svc.restore_all()

        assert len(restored) == 3
        assert (project / "a.py").read_text(encoding="utf-8") == "old_a"
        assert (project / "b.py").read_text(encoding="utf-8") == "old_b"
        assert not (project / "c.py").exists()


class TestManifest:
    async def test_manifest_format(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"x.py": "content"})
        svc = SnapshotService(project, "m5a6n7i8")

        svc.snapshot(str(project / "x.py"))

        manifest_path = project / ".mini_cc" / "snapshots" / "m5a6n7i8" / "_manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["agent_id"] == "m5a6n7i8"
        assert "x.py" in data["files"]

    async def test_manifest_persists_across_instances(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"y.py": "data"})
        SnapshotService(project, "p1e2r3s4").snapshot(str(project / "y.py"))

        svc2 = SnapshotService(project, "p1e2r3s4")
        assert "y.py" in svc2.list_snapshots()


class TestOnToolExecute:
    async def test_intercepts_file_edit(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"src/main.py": "code"})
        svc = SnapshotService(project, "h1o2o3k4")

        svc.on_tool_execute("file_edit", {"file_path": str(project / "src" / "main.py")})

        assert "src/main.py" in svc.list_snapshots()

    async def test_intercepts_file_write(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        svc = SnapshotService(project, "h5w6r7i8")

        svc.on_tool_execute("file_write", {"file_path": str(project / "new.py")})

        assert "new.py" in svc.list_snapshots()

    async def test_ignores_other_tools(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"a.py": "x"})
        svc = SnapshotService(project, "i1g2n3r4")

        svc.on_tool_execute("bash", {"command": "echo hi"})
        svc.on_tool_execute("file_read", {"file_path": str(project / "a.py")})
        svc.on_tool_execute("glob", {"pattern": "**/*.py"})

        assert len(svc.list_snapshots()) == 0


class TestCleanup:
    async def test_cleanup_removes_snapshot_dir(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path, {"a.py": "v1"})
        svc = SnapshotService(project, "c9l8e7a6")

        svc.snapshot(str(project / "a.py"))
        assert (project / ".mini_cc" / "snapshots" / "c9l8e7a6").exists()

        svc.cleanup()

        assert not (project / ".mini_cc" / "snapshots" / "c9l8e7a6").exists()

    async def test_cleanup_nonexistent_is_noop(self, tmp_path: Path) -> None:
        project = _make_project(tmp_path)
        svc = SnapshotService(project, "n0o1n2e3")
        svc.cleanup()

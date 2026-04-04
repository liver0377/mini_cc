from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


class SnapshotService:
    def __init__(self, project_root: Path, agent_id: str) -> None:
        self._project_root = project_root
        self._agent_id = agent_id
        self._snapshot_dir = project_root / ".mini_cc" / "snapshots" / agent_id
        self._manifest_path = self._snapshot_dir / "_manifest.json"
        self._files: dict[str, str] = {}
        if self._manifest_path.exists():
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            self._files = data.get("files", {})

    def on_tool_execute(self, tool_name: str, args: dict[str, Any]) -> None:
        if tool_name in ("file_edit", "file_write"):
            file_path = args.get("file_path", "")
            if file_path:
                self.snapshot(file_path)

    def snapshot(self, file_path: str) -> None:
        abs_path = Path(file_path)
        try:
            rel_path = abs_path.relative_to(self._project_root)
        except ValueError:
            return
        rel_key = str(rel_path)
        if rel_key in self._files:
            return
        if abs_path.exists():
            snapshot_path = self._snapshot_dir / rel_path
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_path, snapshot_path)
            self._files[rel_key] = "modified"
        else:
            self._files[rel_key] = "created"
        self._save_manifest()

    def restore_all(self) -> list[str]:
        restored: list[str] = []
        for rel_path, status in self._files.items():
            abs_path = self._project_root / rel_path
            if status == "modified":
                snapshot_path = self._snapshot_dir / rel_path
                if snapshot_path.exists():
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snapshot_path, abs_path)
                    restored.append(rel_path)
            elif status == "created":
                if abs_path.exists():
                    abs_path.unlink()
                restored.append(rel_path)
        return restored

    def list_snapshots(self) -> dict[str, str]:
        return dict(self._files)

    def cleanup(self) -> None:
        if self._snapshot_dir.exists():
            shutil.rmtree(self._snapshot_dir, ignore_errors=True)

    def _save_manifest(self) -> None:
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "agent_id": self._agent_id,
            "files": self._files,
        }
        self._manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

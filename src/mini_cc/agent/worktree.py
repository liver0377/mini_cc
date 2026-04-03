from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeService:
    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._worktrees_dir = project_root / ".mini_cc" / "worktrees"

    @property
    def worktrees_dir(self) -> Path:
        return self._worktrees_dir

    @property
    def output_dir(self) -> Path:
        return self._worktrees_dir.parent / "tasks"

    def create(self, agent_id: str, ref: str = "HEAD") -> Path:
        worktree_path = self._worktrees_dir / agent_id
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), ref],
            capture_output=True,
            text=True,
            cwd=str(self._project_root),
            timeout=30,
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"git worktree add failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        return worktree_path

    def remove(self, agent_id: str) -> None:
        worktree_path = self._worktrees_dir / agent_id
        if not worktree_path.exists():
            return

        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
            text=True,
            cwd=str(self._project_root),
            timeout=30,
        )

        if result.returncode != 0 and worktree_path.exists():
            import shutil
            shutil.rmtree(worktree_path, ignore_errors=True)

        self._prune()

    def cleanup_output(self, agent_id: str) -> None:
        output_path = self.output_dir / f"{agent_id}.output"
        if output_path.exists():
            output_path.unlink(missing_ok=True)

    def cleanup_all(self, agent_ids: list[str]) -> None:
        for agent_id in agent_ids:
            self.remove(agent_id)
            self.cleanup_output(agent_id)

    def list_active(self) -> list[str]:
        if not self._worktrees_dir.exists():
            return []
        return [p.name for p in self._worktrees_dir.iterdir() if p.is_dir()]

    def ensure_gitignore(self) -> None:
        gitignore = self._project_root / ".gitignore"
        entry = ".mini_cc/worktrees/"
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8")
            if entry in content:
                return
            if not content.endswith("\n"):
                content += "\n"
            content += entry + "\n"
        else:
            content = entry + "\n"
        gitignore.write_text(content, encoding="utf-8")

    def _prune(self) -> None:
        subprocess.run(
            ["git", "worktree", "prune"],
            capture_output=True,
            text=True,
            cwd=str(self._project_root),
            timeout=10,
        )


class WorktreeError(Exception):
    pass

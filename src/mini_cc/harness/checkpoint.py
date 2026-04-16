from __future__ import annotations

import re
from pathlib import Path

from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.models import RunState

_RUNS_DIR = Path.cwd() / ".mini_cc" / "runs"


def _sanitize_filename(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", name).strip("._")
    return sanitized or "artifact"


class CheckpointStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or _RUNS_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self._base_dir / run_id

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "state.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def summary_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "summary.md"

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def checkpoints_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "checkpoints"

    def save_state(self, state: RunState) -> Path:
        run_dir = self.run_dir(state.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path(state.run_id).write_text(state.model_dump_json(indent=2), encoding="utf-8")
        self.summary_path(state.run_id).write_text(state.latest_summary + "\n", encoding="utf-8")
        return self.state_path(state.run_id)

    def load_state(self, run_id: str) -> RunState:
        return RunState.model_validate_json(self.state_path(run_id).read_text(encoding="utf-8"))

    def append_event(self, event: HarnessEvent) -> Path:
        path = self.events_path(event.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fd:
            fd.write(event.model_dump_json())
            fd.write("\n")
        return path

    def load_events(self, run_id: str) -> list[HarnessEvent]:
        path = self.events_path(run_id)
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [HarnessEvent.model_validate_json(line) for line in lines if line]

    def save_artifact(self, run_id: str, name: str, content: str) -> str:
        artifact_dir = self.artifacts_dir(run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / _sanitize_filename(name)
        path.write_text(content, encoding="utf-8")
        return str(path)

    def save_checkpoint(self, state: RunState, label: str) -> str:
        checkpoint_dir = self.checkpoints_dir(state.run_id)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = checkpoint_dir / f"{_sanitize_filename(label)}.json"
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        return str(path)

    def latest_run_id(self) -> str | None:
        candidates: list[tuple[float, str]] = []
        for state_path in self._base_dir.glob("*/state.json"):
            candidates.append((state_path.stat().st_mtime, state_path.parent.name))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

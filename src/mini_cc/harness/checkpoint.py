from __future__ import annotations

import re
from pathlib import Path
from typing import TypeVar

from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.iteration import IterationReview, IterationSnapshot
from mini_cc.harness.models import RunState, TraceSpan

_RUNS_DIR = Path.cwd() / ".mini_cc" / "runs"
JsonLineModel = TypeVar("JsonLineModel", HarnessEvent, IterationSnapshot, IterationReview, TraceSpan)


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

    def journal_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "journal.md"

    def documentation_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "Documentation.md"

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def checkpoints_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "checkpoints"

    def snapshots_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "iteration_snapshots.jsonl"

    def reviews_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "iteration_reviews.jsonl"

    def trace_spans_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "trace_spans.jsonl"

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
        return self._append_jsonl(path, event.model_dump_json())

    def append_iteration_snapshot(self, snapshot: IterationSnapshot) -> Path:
        return self._append_jsonl(self.snapshots_path(snapshot.run_id), snapshot.model_dump_json())

    def append_iteration_review(self, review: IterationReview) -> Path:
        return self._append_jsonl(self.reviews_path(review.run_id), review.model_dump_json())

    def append_trace_span(self, span: TraceSpan) -> Path:
        return self._append_jsonl(self.trace_spans_path(span.run_id), span.model_dump_json())

    def append_journal_entry(self, run_id: str, content: str) -> Path:
        path = self.journal_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fd:
            fd.write(content)
        return path

    def save_documentation(self, run_id: str, content: str) -> Path:
        path = self.documentation_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def load_events(self, run_id: str) -> list[HarnessEvent]:
        return self._load_jsonl(self.events_path(run_id), HarnessEvent)

    def load_iteration_snapshots(self, run_id: str) -> list[IterationSnapshot]:
        return self._load_jsonl(self.snapshots_path(run_id), IterationSnapshot)

    def load_iteration_reviews(self, run_id: str) -> list[IterationReview]:
        return self._load_jsonl(self.reviews_path(run_id), IterationReview)

    def load_trace_spans(self, run_id: str) -> list[TraceSpan]:
        return self._load_jsonl(self.trace_spans_path(run_id), TraceSpan)

    def latest_iteration_snapshot(self, run_id: str) -> IterationSnapshot | None:
        snapshots = self.load_iteration_snapshots(run_id)
        if not snapshots:
            return None
        return snapshots[-1]

    def latest_iteration_review(self, run_id: str) -> IterationReview | None:
        reviews = self.load_iteration_reviews(run_id)
        if not reviews:
            return None
        return reviews[-1]

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

    def list_states(self) -> list[RunState]:
        states: list[tuple[float, RunState]] = []
        for state_path in self._base_dir.glob("*/state.json"):
            try:
                state = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))
            except OSError:
                continue
            states.append((state_path.stat().st_mtime, state))
        states.sort(key=lambda item: item[0], reverse=True)
        return [state for _, state in states]

    def _append_jsonl(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fd:
            fd.write(content)
            fd.write("\n")
        return path

    def _load_jsonl(
        self,
        path: Path,
        model: type[JsonLineModel],
    ) -> list[JsonLineModel]:
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [model.model_validate_json(line) for line in lines if line]

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import cast

from mini_cc.harness.models import TraceSpan
from mini_cc.models import (
    AgentCompletionEvent,
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    TextDelta,
    ToolCallDelta,
    ToolCallStart,
    ToolResultEvent,
)


class QueryDiagnostics:
    __slots__ = (
        "started_at",
        "last_event_at",
        "last_event_type",
        "first_event_at",
        "first_text_delta_at",
        "text_delta_count",
        "tool_call_names",
        "tool_result_count",
        "agent_event_count",
        "total_text_chars",
        "turn_count",
        "message_count",
        "turn_starts",
        "turn_llm_durations",
        "turn_tool_durations",
        "current_turn_start",
        "current_turn_phase",
        "current_turn_phase_start",
        "active_tool_calls",
        "tool_trace",
        "active_agent_tools",
        "agent_started_at",
        "max_inter_event_gap",
        "prev_event_at",
        "error_type",
        "error_detail",
        "trace_spans",
    )

    def __init__(self, message_count: int = 0, turn_count: int = 0) -> None:
        self.started_at: float = 0.0
        self.last_event_at: float = 0.0
        self.last_event_type: str = ""
        self.first_event_at: float = 0.0
        self.first_text_delta_at: float = 0.0
        self.text_delta_count: int = 0
        self.tool_call_names: list[str] = []
        self.tool_result_count: int = 0
        self.agent_event_count: int = 0
        self.total_text_chars: int = 0
        self.turn_count: int = turn_count
        self.message_count: int = message_count
        self.turn_starts: list[float] = []
        self.turn_llm_durations: list[float] = []
        self.turn_tool_durations: list[float] = []
        self.current_turn_start: float = 0.0
        self.current_turn_phase: str = ""
        self.current_turn_phase_start: float = 0.0
        self.active_tool_calls: dict[str, tuple[str, float, str]] = {}
        self.tool_trace: list[str] = []
        self.active_agent_tools: dict[str, list[tuple[str, float]]] = {}
        self.agent_started_at: dict[str, float] = {}
        self.max_inter_event_gap: float = 0.0
        self.prev_event_at: float = 0.0
        self.error_type: str = ""
        self.error_detail: str = ""
        self.trace_spans: list[TraceSpan] = []

    def record_event(self, event: Event) -> None:
        now = time.monotonic()
        if self.first_event_at == 0.0:
            self.first_event_at = now
        if self.prev_event_at > 0.0:
            gap = now - self.prev_event_at
            if gap > self.max_inter_event_gap:
                self.max_inter_event_gap = gap
        self.prev_event_at = now
        self.last_event_at = now
        self.last_event_type = type(event).__name__
        if isinstance(event, TextDelta):
            if self.first_text_delta_at == 0.0:
                self.first_text_delta_at = now
            self.text_delta_count += 1
            self.total_text_chars += len(event.content)
            if self.current_turn_phase == "tool":
                self._finish_turn_inner(now)
            if self.current_turn_phase == "":
                self.current_turn_phase = "llm"
                self.current_turn_start = now
                self.turn_starts.append(now)
                self.current_turn_phase_start = now
        elif isinstance(event, ToolResultEvent):
            self.tool_result_count += 1
            self.tool_call_names.append(event.name)
            self._record_tool_result(event, now)
            if self.current_turn_phase == "llm":
                llm_dur = now - self.current_turn_start
                self.turn_llm_durations.append(llm_dur)
                self.current_turn_phase = "tool"
                self.current_turn_phase_start = now
        elif isinstance(event, ToolCallStart):
            self.active_tool_calls[event.tool_call_id] = (event.name, now, "")
            if self.current_turn_phase == "":
                self.current_turn_phase = "llm"
                self.current_turn_start = now
                self.turn_starts.append(now)
                self.current_turn_phase_start = now
        elif isinstance(event, ToolCallDelta):
            existing = self.active_tool_calls.get(event.tool_call_id)
            if existing is not None:
                name, started_at, arguments = existing
                self.active_tool_calls[event.tool_call_id] = (name, started_at, arguments + event.arguments_json_delta)
        elif isinstance(event, AgentStartEvent):
            self.agent_event_count += 1
            self.agent_started_at[event.agent_id] = now
            self.tool_trace.append(f"agent[{event.agent_id[:8]}].start")
        elif isinstance(event, AgentToolCallEvent):
            self.agent_event_count += 1
            self.active_agent_tools.setdefault(event.agent_id, []).append((event.tool_name, now))
        elif isinstance(event, AgentToolResultEvent):
            self.agent_event_count += 1
            self._record_agent_tool_result(event, now)
        elif isinstance(event, AgentCompletionEvent):
            self.agent_event_count += 1
            started_at = self.agent_started_at.pop(event.agent_id, 0.0)
            elapsed = now - started_at if started_at > 0.0 else 0.0
            self.tool_trace.append(
                f"agent[{event.agent_id[:8]}].complete(success={str(event.success).lower()},elapsed={elapsed:.1f}s)"
            )
        else:
            agent_id = cast(str | None, getattr(event, "agent_id", None))
            if agent_id is not None:
                self.agent_event_count += 1

    def _finish_turn_inner(self, now: float) -> None:
        if self.current_turn_start == 0.0:
            return
        if self.current_turn_phase == "tool":
            tool_dur = now - self.current_turn_phase_start
            self.turn_tool_durations.append(tool_dur)
            if len(self.turn_llm_durations) < len(self.turn_starts):
                self.turn_llm_durations.append(0.0)
        elif self.current_turn_phase == "llm":
            llm_dur = now - self.current_turn_start
            self.turn_llm_durations.append(llm_dur)
            self.turn_tool_durations.append(0.0)
        self.current_turn_start = now
        self.current_turn_phase = ""
        self.current_turn_phase_start = 0.0

    def finish_turn(self) -> None:
        if self.current_turn_start == 0.0:
            return
        self._finish_turn_inner(time.monotonic())

    def to_metadata(self, timeout_seconds: int | None = None) -> dict[str, str]:
        elapsed_end = self.last_event_at if self.last_event_at > 0.0 else None
        md: dict[str, str] = {
            "trace_elapsed_ms": str(self._duration_ms(self.started_at, elapsed_end)),
            "trace_last_event_type": self.last_event_type or "(none)",
            "trace_text_delta_count": str(self.text_delta_count),
            "trace_tool_result_count": str(self.tool_result_count),
            "trace_agent_event_count": str(self.agent_event_count),
            "trace_total_text_chars": str(self.total_text_chars),
            "trace_message_count": str(self.message_count),
            "trace_turn_count": str(self.turn_count),
            "trace_max_inter_event_gap_ms": str(max(0, int(self.max_inter_event_gap * 1000))),
        }
        if timeout_seconds is not None:
            md["timeout_seconds"] = str(timeout_seconds)
        if self.first_event_at > 0.0 and self.started_at > 0.0:
            md["trace_first_event_ms"] = str(self._duration_ms(self.started_at, self.first_event_at))
        if self.first_text_delta_at > 0.0 and self.started_at > 0.0:
            md["trace_first_token_ms"] = str(self._duration_ms(self.started_at, self.first_text_delta_at))
        if self.turn_llm_durations:
            md["trace_turn_llm_ms"] = ",".join(str(max(0, int(d * 1000))) for d in self.turn_llm_durations)
        if self.turn_tool_durations:
            md["trace_turn_tool_ms"] = ",".join(str(max(0, int(d * 1000))) for d in self.turn_tool_durations)
        if self.tool_trace:
            md["trace_tool_span_count"] = str(len(self.tool_trace))
            md["trace_tool_outline"] = self._fmt_tool_trace()
        if self.tool_call_names:
            md["trace_tool_names"] = ",".join(self.tool_call_names)
        if self.error_type:
            md["trace_error_type"] = self.error_type
        if self.error_detail:
            md["trace_error_detail"] = self.error_detail[:500]
        return md

    def _fmt_elapsed(self) -> str:
        if self.started_at == 0.0:
            return ""
        end = self.last_event_at if self.last_event_at > 0.0 else time.monotonic()
        return f"{end - self.started_at:.1f}s"

    def _duration_ms(self, start: float, end: float | None) -> int:
        if start <= 0.0:
            return 0
        final_end = end if end is not None and end > 0.0 else time.monotonic()
        return max(0, int((final_end - start) * 1000))

    def summarize_timeout(self, timeout_seconds: int) -> str:
        elapsed = self._fmt_elapsed()
        turn_summary = self._fmt_turn_summary()
        if self.last_event_type == "":
            return (
                f"Step timed out after {timeout_seconds}s. "
                f"LLM provider never returned any event (elapsed: {elapsed}). "
                f"Context had {self.message_count} messages, {self.turn_count} prior turns."
            )
        if self.text_delta_count == 0 and self.tool_result_count == 0:
            return (
                f"Step timed out after {timeout_seconds}s (elapsed: {elapsed}). "
                f"No text or tool output received. "
                f"Last event: {self.last_event_type}. "
                f"Agent events: {self.agent_event_count}. "
                f"Context had {self.message_count} messages. "
                f"Max inter-event gap: {self.max_inter_event_gap:.1f}s."
            )
        return (
            f"Step timed out after {timeout_seconds}s (elapsed: {elapsed}). "
            f"Received {self.text_delta_count} text deltas ({self.total_text_chars} chars), "
            f"{self.tool_result_count} tool results ({','.join(self.tool_call_names)}). "
            f"Last event: {self.last_event_type}. "
            f"Context: {self.message_count} messages. "
            f"Max inter-event gap: {self.max_inter_event_gap:.1f}s. "
            f"{turn_summary}"
            f"{self._fmt_tool_trace_for_summary()}"
        )

    def _fmt_turn_summary(self) -> str:
        if not self.turn_llm_durations:
            return ""
        parts: list[str] = []
        for i, llm_d in enumerate(self.turn_llm_durations):
            tool_d = self.turn_tool_durations[i] if i < len(self.turn_tool_durations) else 0.0
            parts.append(f"turn{i + 1}(llm={llm_d:.1f}s,tool={tool_d:.1f}s)")
        return "Per-turn timing: " + " -> ".join(parts)

    def _record_tool_result(self, event: ToolResultEvent, now: float) -> None:
        existing = self.active_tool_calls.pop(event.tool_call_id, None)
        if existing is None:
            self.tool_trace.append(f"{event.name}=unknown")
            self.trace_spans.append(
                TraceSpan(
                    span_id=f"tool-{len(self.trace_spans) + 1}",
                    run_id="",
                    kind="tool",
                    name=event.name,
                    status="success" if event.success else "failed",
                    end_at=datetime.now(UTC).isoformat(),
                    duration_ms=0,
                )
            )
            return
        name, started_at, arguments = existing
        elapsed = now - started_at
        label = self._tool_label(name, arguments)
        self.tool_trace.append(f"{label}={elapsed:.1f}s")
        self.trace_spans.append(
            TraceSpan(
                span_id=f"tool-{len(self.trace_spans) + 1}",
                run_id="",
                kind="tool",
                name=label,
                status="success" if event.success else "failed",
                end_at=datetime.now(UTC).isoformat(),
                duration_ms=int(elapsed * 1000),
            )
        )

    def _record_agent_tool_result(self, event: AgentToolResultEvent, now: float) -> None:
        active = self.active_agent_tools.get(event.agent_id, [])
        index = next((i for i, item in enumerate(active) if item[0] == event.tool_name), -1)
        if index >= 0:
            _, started_at = active.pop(index)
            elapsed = now - started_at
        else:
            elapsed = 0.0
        if not active:
            self.active_agent_tools.pop(event.agent_id, None)
        agent_key = event.agent_id[:8]
        self.tool_trace.append(
            f"agent[{agent_key}].{event.tool_name}(success={str(event.success).lower()},elapsed={elapsed:.1f}s)"
        )
        self.trace_spans.append(
            TraceSpan(
                span_id=f"agent-tool-{len(self.trace_spans) + 1}",
                run_id="",
                kind="tool",
                name=f"agent[{agent_key}].{event.tool_name}",
                status="success" if event.success else "failed",
                end_at=datetime.now(UTC).isoformat(),
                duration_ms=int(elapsed * 1000),
                metadata={"agent_id": event.agent_id},
            )
        )

    def _tool_label(self, name: str, arguments: str) -> str:
        compact = arguments.strip()
        if not compact:
            return name
        try:
            parsed = json.loads(compact)
        except (json.JSONDecodeError, ValueError):
            return name
        if not isinstance(parsed, dict):
            return name
        for key in ("file_path", "path", "command", "pattern", "prompt"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                normalized = value.strip().replace("\n", " ")
                return f"{name}({normalized[:60]})"
        if name == "agent":
            readonly = parsed.get("readonly")
            if isinstance(readonly, bool):
                return f"{name}(readonly={str(readonly).lower()})"
        return name

    def _fmt_tool_trace(self) -> str:
        trace = " -> ".join(self.tool_trace)
        return trace[:4000]

    def _fmt_tool_trace_for_summary(self) -> str:
        if not self.tool_trace:
            return ""
        trace = self._fmt_tool_trace()
        return f" Trace: {trace}."

    def build_trace_spans(
        self,
        *,
        run_id: str,
        step_id: str,
        work_item_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> list[TraceSpan]:
        return [
            span.model_copy(
                update={
                    "span_id": f"{step_id}-{work_item_id or 'step'}-trace-{index}",
                    "run_id": run_id,
                    "step_id": step_id,
                    "work_item_id": work_item_id,
                    "parent_span_id": parent_span_id,
                }
            )
            for index, span in enumerate(self.trace_spans, start=1)
        ]

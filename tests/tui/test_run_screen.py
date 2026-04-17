from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from mini_cc.harness import CheckpointStore, RunHarness, RunState, RunStatus, Step, StepKind, StepStatus
from mini_cc.harness.iteration import IterationOutcome, IterationReview, IterationScore
from mini_cc.harness.models import TraceSpan, format_local_time
from mini_cc.tui.screens.run_screen import (
    _RUN_STATUS_COLORS,
    _RUN_STATUS_ICONS,
    _STEP_STATUS_ICONS,
    RunScreen,
)


def _make_run_state(run_id: str = "abcdef123456", status: RunStatus = RunStatus.RUNNING) -> RunState:
    return RunState(
        run_id=run_id,
        goal="Fix failing tests",
        status=status,
        phase="edit_code",
        steps=[
            Step(
                id="step-1",
                kind=StepKind.ANALYZE_REPO,
                title="Analyze",
                goal="Analyze repo",
                status=StepStatus.SUCCEEDED,
                summary="Read the repo",
            ),
            Step(
                id="step-2",
                kind=StepKind.EDIT_CODE,
                title="Execute",
                goal="Edit code",
                status=StepStatus.IN_PROGRESS,
                summary="Editing files",
            ),
        ],
        current_step_id="step-2",
        latest_summary="Editing files",
    )


class TestRunScreenInit:
    def test_status_icons_coverage(self):
        for status in RunStatus:
            assert status in _RUN_STATUS_ICONS

    def test_status_colors_coverage(self):
        for status in RunStatus:
            assert status in _RUN_STATUS_COLORS

    def test_step_status_icons_coverage(self):
        for status in StepStatus:
            assert status in _STEP_STATUS_ICONS

    def test_screen_creates_with_store(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        screen = RunScreen(store)
        assert screen._store is store
        assert screen._runs == []
        assert screen._selected_idx == -1


class TestRunScreenDataOps:
    def test_refresh_loads_runs_from_store(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)

        screen = RunScreen(store)
        screen._runs = store.list_states()

        assert len(screen._runs) == 1
        assert screen._runs[0].run_id == run.run_id

    def test_cursor_up_wraps(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        screen = RunScreen(store)
        screen._runs = [_make_run_state("run1"), _make_run_state("run2")]
        screen._selected_idx = 0

        screen.action_cursor_up()

        assert screen._selected_idx == 1

    def test_cursor_down_wraps(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        screen = RunScreen(store)
        screen._runs = [_make_run_state("run1"), _make_run_state("run2")]
        screen._selected_idx = 1

        screen.action_cursor_down()

        assert screen._selected_idx == 0

    def test_cursor_no_runs(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        screen = RunScreen(store)

        screen.action_cursor_up()
        screen.action_cursor_down()

        assert screen._selected_idx == -1

    def test_cancel_run_calls_harness(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        harness = MagicMock(spec=RunHarness)
        screen = RunScreen(store, harness)
        screen._runs = [_make_run_state()]
        screen._selected_idx = 0

        screen.action_cancel_run()

        harness.cancel.assert_called_once_with("abcdef123456")

    def test_cancel_completed_run_does_nothing(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        harness = MagicMock(spec=RunHarness)
        screen = RunScreen(store, harness)
        screen._runs = [_make_run_state(status=RunStatus.COMPLETED)]
        screen._selected_idx = 0

        screen.action_cancel_run()

        harness.cancel.assert_not_called()

    def test_review_lines_render_recent_reviews(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.append_iteration_review(
            IterationReview(
                run_id=run.run_id,
                step_id="step-2",
                outcome=IterationOutcome.IMPROVED,
                score=IterationScore(total=3, success_signal=3),
                root_cause="verification passed",
                recommended_step_kind="finalize",
            )
        )

        screen = RunScreen(store)

        lines = screen._review_lines(run.run_id)

        assert len(lines) == 1
        assert "verification passed" in lines[0]
        assert "finalize" in lines[0]

    def test_journal_lines_render_tail(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.append_journal_entry(run.run_id, "## step-1 `analyze_repo`\n- Outcome: improved\n")

        screen = RunScreen(store)

        lines = screen._journal_lines(run.run_id)

        assert any("Outcome: improved" in line for line in lines)

    def test_documentation_lines_render_preview(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.save_documentation(
            run.run_id,
            "# Run abcdef Documentation\n\n## 基本信息\n\n| 项目 | 值 |\n|------|------|\n| Run ID | abcdef |\n",
        )

        screen = RunScreen(store)

        lines = screen._documentation_lines(run.run_id)

        assert any("## 基本信息" in line for line in lines)

    def test_trace_lines_render_nested_spans(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.append_trace_span(
            TraceSpan(
                span_id="step-1",
                run_id=run.run_id,
                step_id="step-1",
                kind="step",
                name="edit_code",
                status="success",
                duration_ms=2100,
                summary="edited files",
            )
        )
        store.append_trace_span(
            TraceSpan(
                span_id="tool-1",
                run_id=run.run_id,
                step_id="step-1",
                parent_span_id="step-1",
                kind="tool",
                name="file_write",
                status="success",
                duration_ms=120,
                summary="wrote parser.py",
            )
        )

        screen = RunScreen(store)

        lines = screen._trace_lines(run.run_id)

        assert len(lines) == 2
        assert "step:edit_code" in lines[0]
        assert "2.1s" in lines[0]
        assert "tool:file_write" in lines[1]
        assert "120ms" in lines[1]

    def test_show_detail_formats_times_in_local_timezone(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        run.created_at = "2026-04-17T02:33:25+00:00"
        run.updated_at = "2026-04-17T02:49:33+00:00"
        store.save_state(run)

        screen = RunScreen(store)

        detail = MagicMock()
        detail.set_class = MagicMock()
        screen.query_one = MagicMock(return_value=detail)
        screen._event_lines = MagicMock(return_value=[])
        screen._review_lines = MagicMock(return_value=[])
        screen._journal_lines = MagicMock(return_value=[])
        screen._documentation_lines = MagicMock(return_value=[])

        screen._show_detail(run)

        rendered = detail.update.call_args.args[0]
        assert f"Created: {format_local_time(run.created_at)}" in rendered
        assert f"Updated: {format_local_time(run.updated_at)}" in rendered

    def test_event_lines_include_resume_metadata(self, tmp_path):
        from mini_cc.harness.events import HarnessEvent

        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.append_event(
            HarnessEvent(
                event_type="run_resumed",
                run_id=run.run_id,
                message="invalidated 1 inflight agents; inserted replanning step",
                data={"invalidated_agents": "1", "decision": "resume_replan"},
            )
        )

        screen = RunScreen(store)

        lines = screen._event_lines(run.run_id)

        assert any("invalidated_agents=1" in line for line in lines)
        assert any("decision=resume_replan" in line for line in lines)

    def test_event_lines_include_trace_metadata(self, tmp_path):
        from mini_cc.harness.events import HarnessEvent

        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.append_event(
            HarnessEvent(
                event_type="step_completed",
                run_id=run.run_id,
                step_id="step-2",
                message="ok",
                data={
                    "decision": "continue",
                    "trace_span_count": "3",
                    "trace_tool_count": "2",
                    "trace_agent_count": "1",
                },
            )
        )

        screen = RunScreen(store)

        lines = screen._event_lines(run.run_id)

        assert any("trace_spans=3" in line for line in lines)
        assert any("tools=2" in line for line in lines)
        assert any("agents=1" in line for line in lines)

    def test_event_lines_include_trace_timing(self, tmp_path):
        from mini_cc.harness.events import HarnessEvent

        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.append_event(
            HarnessEvent(
                event_type="step_completed",
                run_id=run.run_id,
                step_id="step-2",
                message="ok",
                data={
                    "trace_elapsed_ms": "2450",
                    "trace_first_event_ms": "120",
                    "trace_first_token_ms": "340",
                },
            )
        )

        screen = RunScreen(store)

        lines = screen._event_lines(run.run_id)

        assert any("elapsed=2.5s" in line for line in lines)
        assert any("first_event=120ms" in line for line in lines)
        assert any("first_token=340ms" in line for line in lines)
        assert all("diag_" not in line for line in lines)

    def test_event_lines_include_scheduler_metadata(self, tmp_path):
        from mini_cc.harness.events import HarnessEvent

        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state()
        store.save_state(run)
        store.append_event(
            HarnessEvent(
                event_type="step_started",
                run_id=run.run_id,
                step_id="step-2",
                message="Execute",
                data={
                    "scheduler_considered": "2",
                    "scheduler_rejected": "step-ro",
                },
            )
        )

        screen = RunScreen(store)

        lines = screen._event_lines(run.run_id)

        assert any("considered=2" in line for line in lines)
        assert any("rejected=step-ro" in line for line in lines)

    async def test_resume_run_calls_harness(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state(status=RunStatus.RUNNING)
        store.save_state(run)
        resumed = _make_run_state(run_id=run.run_id, status=RunStatus.COMPLETED)
        harness = MagicMock(spec=RunHarness)
        harness.resume = AsyncMock(return_value=resumed)

        screen = RunScreen(store, harness)
        screen._runs = [run]
        screen._selected_idx = 0
        screen.call_after_refresh = MagicMock()

        await screen.action_resume_run()

        harness.resume.assert_called_once_with(run.run_id)

    async def test_resume_run_ignores_terminal_run(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        run = _make_run_state(status=RunStatus.BLOCKED)
        store.save_state(run)
        harness = MagicMock(spec=RunHarness)
        harness.resume = AsyncMock()

        screen = RunScreen(store, harness)
        screen._runs = [run]
        screen._selected_idx = 0

        await screen.action_resume_run()

        harness.resume.assert_not_called()

    def test_notify_run_selected_calls_callback(self, tmp_path):
        store = CheckpointStore(base_dir=tmp_path)
        selected: list[str] = []

        def _callback(run: RunState) -> None:
            selected.append(run.run_id)

        run = _make_run_state()
        screen = RunScreen(store, on_run_selected=_callback)

        screen._notify_run_selected(run)

        assert selected == [run.run_id]

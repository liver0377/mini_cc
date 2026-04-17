from __future__ import annotations

from datetime import datetime

from mini_cc.harness.audit import TaskAuditRegistry
from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.iteration import IterationOutcome, IterationReview, IterationSnapshot
from mini_cc.harness.models import RunState, SchedulerDecisionRecord, StepKind, StepStatus, TraceSpan, format_local_time


class RunDocGenerator:
    def __init__(self, task_audit_registry: TaskAuditRegistry | None = None) -> None:
        self._task_audit_registry = task_audit_registry or TaskAuditRegistry()

    def generate(self, run_state: RunState, store: CheckpointStore) -> str:
        events = store.load_events(run_state.run_id)
        snapshots = store.load_iteration_snapshots(run_state.run_id)
        reviews = store.load_iteration_reviews(run_state.run_id)
        scheduler_decisions = store.load_scheduler_decisions(run_state.run_id)
        trace_spans = store.load_trace_spans(run_state.run_id)
        review_map = {review.step_id: review for review in reviews}

        sections = [
            self._render_basic_info(run_state, events),
            self._render_step_timeline(run_state, review_map),
            self._render_trace_summary(trace_spans),
            self._render_timing_summary(events),
            self._render_scheduler_summary(scheduler_decisions),
            self._render_score_trend(reviews),
            self._render_agent_summary(run_state),
            self._render_resource_usage(run_state),
            self._render_quality_assessment(run_state, reviews),
            self._render_decisions(events),
            self._render_task_audit(run_state, snapshots),
            self._render_lessons_learned(run_state, snapshots, reviews),
        ]
        return f"# Run {run_state.run_id} Documentation\n\n" + "\n\n".join(sections) + "\n"

    def _render_basic_info(self, run_state: RunState, events: list[HarnessEvent]) -> str:
        started_at = self._parse_time(run_state.started_at or run_state.created_at)
        ended_at = self._parse_time(run_state.updated_at)
        success_count = sum(1 for step in run_state.steps if step.status == StepStatus.SUCCEEDED)
        failure_count = sum(
            1 for step in run_state.steps if step.status in {StepStatus.FAILED_RETRYABLE, StepStatus.FAILED_TERMINAL}
        )
        terminal_reason = self._terminal_reason(events)
        lines = [
            "## 基本信息",
            "",
            "| 项目 | 值 |",
            "|------|------|",
            f"| Run ID | {run_state.run_id} |",
            f"| 目标 | {run_state.goal} |",
            f"| 状态 | {run_state.status.value} |",
            f"| 阶段 | {run_state.phase} |",
            f"| 创建时间 | {format_local_time(run_state.created_at)} |",
            f"| 结束时间 | {format_local_time(run_state.updated_at)} |",
            f"| 运行耗时 | {self._format_duration(started_at, ended_at)} |",
            f"| 终止原因 | {terminal_reason} |",
            f"| 总 Step 数 | {len(run_state.steps)} |",
            f"| 成功 / 失败 | {success_count} / {failure_count} |",
        ]
        return "\n".join(lines)

    def _render_step_timeline(self, run_state: RunState, review_map: dict[str, IterationReview]) -> str:
        lines = [
            "## Step 执行时间线",
            "",
            "| # | ID | 类型 | 状态 | 迭代结果 | 摘要 |",
            "|---|------|------|------|----------|------|",
        ]
        for index, step in enumerate(run_state.steps, start=1):
            review = review_map.get(step.id)
            outcome = review.outcome.value if review is not None else "-"
            summary = (step.summary or step.goal).replace("\n", " ")
            lines.append(
                f"| {index} | {step.id} | {step.kind.value} | {step.status.value} | {outcome} | {summary[:80]} |"
            )
        return "\n".join(lines)

    def _render_trace_summary(self, spans: list[TraceSpan]) -> str:
        lines = [
            "## 执行 Trace 摘要",
            "",
            "| 指标 | 值 |",
            "|------|------|",
            f"| Span 总数 | {len(spans)} |",
            f"| Step / WorkItem | {self._count_kind(spans, 'step')} / {self._count_kind(spans, 'work_item')} |",
            f"| Agent / Tool | {self._count_kind(spans, 'agent')} / {self._count_kind(spans, 'tool')} |",
        ]
        if not spans:
            lines.extend(["", "暂无结构化 trace。"])
            return "\n".join(lines)

        roots = [span for span in spans if span.parent_span_id is None]
        children_map = self._children_map(spans)
        lines.extend(["", "### 最近执行链路", ""])
        for root in roots[-3:]:
            lines.extend(self._trace_lines(root, children_map, indent=0))
        return "\n".join(lines)

    def _render_score_trend(self, reviews: list[IterationReview]) -> str:
        lines = [
            "## 迭代评分趋势",
            "",
            "| Step | Score | Outcome | 根因 |",
            "|------|-------|---------|------|",
        ]
        if not reviews:
            lines.append("| - | - | - | - |")
            return "\n".join(lines)

        for review in reviews:
            root_cause = review.root_cause.replace("\n", " ")
            lines.append(f"| {review.step_id} | {review.score.total} | {review.outcome.value} | {root_cause[:80]} |")
        return "\n".join(lines)

    def _render_timing_summary(self, events: list[HarnessEvent]) -> str:
        lines = [
            "## 执行时序",
            "",
            "| Step | 总耗时 | 首事件 | 首 Token | LLM 回合 | Tool 回合 |",
            "|------|--------|--------|-----------|----------|-----------|",
        ]
        timed_events = [event for event in events if event.event_type == "step_completed"]
        if not timed_events:
            lines.append("| - | - | - | - | - | - |")
            return "\n".join(lines)
        for event in timed_events[-8:]:
            elapsed = self._fmt_ms_value(event.data.get("trace_elapsed_ms"))
            first_event = self._fmt_ms_value(event.data.get("trace_first_event_ms"))
            first_token = self._fmt_ms_value(event.data.get("trace_first_token_ms"))
            llm_turns = self._fmt_turn_series(event.data.get("trace_turn_llm_ms"))
            tool_turns = self._fmt_turn_series(event.data.get("trace_turn_tool_ms"))
            lines.append(
                f"| {event.step_id or '-'} | {elapsed} | {first_event} | {first_token} | {llm_turns} | {tool_turns} |"
            )
        return "\n".join(lines)

    def _render_scheduler_summary(self, decisions: list[SchedulerDecisionRecord]) -> str:
        lines = [
            "## 调度记录",
            "",
            "| Step | Role | Priority | Considered | Rejected | 原因 |",
            "|------|------|----------|------------|----------|------|",
        ]
        if not decisions:
            lines.append("| - | - | - | - | - | - |")
            return "\n".join(lines)
        for decision in decisions[-8:]:
            rejected = ",".join(decision.rejected_targets) or "-"
            lines.append(
                f"| {decision.step_id} | {decision.selected_role} | {decision.selected_priority} | "
                f"{decision.considered_count} | {rejected} | {decision.reason[:80]} |"
            )
        return "\n".join(lines)

    def _render_agent_summary(self, run_state: RunState) -> str:
        readonly_agents = int(run_state.metadata.get("agents_created_readonly", "0"))
        write_agents = int(run_state.metadata.get("agents_created_write", "0"))
        successful_agents = int(run_state.metadata.get("agents_succeeded", "0"))
        failed_agents = int(run_state.metadata.get("agents_failed", "0"))
        stale_agents = int(run_state.metadata.get("agents_stale", "0"))
        cancelled_agents = int(run_state.metadata.get("agents_cancelled", "0"))
        peak_active = int(run_state.metadata.get("agent_peak_active", str(run_state.active_agent_count)))

        lines = [
            "## 子 Agent 活动摘要",
            "",
            "| 指标 | 值 |",
            "|------|------|",
            f"| 总创建数 | {len(run_state.spawned_agents)} |",
            f"| Readonly / Write | {readonly_agents} / {write_agents} |",
            f"| 成功 / 失败 | {successful_agents} / {failed_agents} |",
            f"| Stale / Cancelled | {stale_agents} / {cancelled_agents} |",
            f"| 活跃 Agent 峰值 | {peak_active} |",
        ]
        if not run_state.spawned_agents:
            lines.extend(["", "本 Run 未使用子 Agent。"])
            return "\n".join(lines)

        lines.extend(
            [
                "",
                "| Agent | 类型 | 来源 Step | Scope | 结果 | Stale | 终止原因 | 摘要 |",
                "|-------|------|-----------|-------|------|-------|----------|------|",
            ]
        )
        for agent in run_state.spawned_agents:
            result = "运行中"
            if agent.success is True:
                result = "成功"
            elif agent.success is False:
                result = "失败"
            reason = agent.termination_reason or "-"
            preview = agent.output_preview.replace("\n", " ")
            lines.append(
                f"| {agent.agent_id} | {'readonly' if agent.readonly else 'write'} | "
                f"{agent.source_step_id or '-'} | {', '.join(agent.scope_paths) or '.'} | "
                f"{result} | {'yes' if agent.is_stale else 'no'} | {reason} | {preview[:50] or '-'} |"
            )
        return "\n".join(lines)

    def _render_resource_usage(self, run_state: RunState) -> str:
        peak_active = int(run_state.metadata.get("agent_peak_active", str(run_state.active_agent_count)))
        lines = [
            "## 资源消耗",
            "",
            "| 资源 | 使用量 | 上限 |",
            "|------|--------|------|",
            f"| 测试执行 | {run_state.test_run_count} | {run_state.budget.max_test_runs} |",
            f"| Bash 命令 | {run_state.bash_command_count} | {run_state.budget.max_bash_commands} |",
            f"| REPLAN 次数 | {run_state.replan_count} | {run_state.retry_policy.max_replan_count} |",
            f"| 活跃 Agent 峰值 | {peak_active} | {run_state.budget.max_active_agents * 2} |",
        ]
        return "\n".join(lines)

    def _render_quality_assessment(self, run_state: RunState, reviews: list[IterationReview]) -> str:
        final_review = reviews[-1] if reviews else None
        unresolved = final_review.next_constraints if final_review is not None else []
        generic_agent_issues = self._generic_agent_issues(run_state)
        lines = [
            "## 质量评估",
            "",
            "| 维度 | 评价 |",
            "|------|------|",
            f"| 目标达成度 | {'完全达成' if run_state.status.value == 'completed' else '未完全达成'} |",
            f"| 最终健康度 | {final_review.outcome.value if final_review is not None else 'unknown'} |",
            f"| 失败计数 | {run_state.failure_count} |",
            f"| 无进展计数 | {run_state.consecutive_no_progress_count} |",
            "",
            "### 未解决问题",
            "",
        ]
        if unresolved:
            lines.extend(f"- {item}" for item in unresolved)
        else:
            lines.append("- 无")
        if generic_agent_issues:
            lines.extend(["", "### Agent 结果风险", ""])
            lines.extend(f"- {item}" for item in generic_agent_issues)
        return "\n".join(lines)

    def _render_decisions(self, events: list[HarnessEvent]) -> str:
        lines = [
            "## 关键决策记录",
            "",
            "| Step | 决策 | 原因 | Active Agents | Trace | 自动插入 |",
            "|------|------|------|---------------|-------|----------|",
        ]
        decision_events = [
            event
            for event in events
            if event.event_type in {"run_failed", "run_timed_out", "step_completed", "run_completed", "run_resumed"}
        ]
        if not decision_events:
            lines.append("| - | - | - | - | - | - |")
            return "\n".join(lines)
        for event in decision_events[-12:]:
            decision = event.data.get("decision", event.event_type)
            reason = event.data.get("decision_reason", event.message).replace("\n", " ")
            active_agents = event.data.get("active_agents", "-")
            trace = self._decision_trace_summary(event)
            inserted = event.data.get("inserted_steps", "-") or "-"
            lines.append(
                f"| {event.step_id or event.event_type} | {decision} | {reason[:60]} | "
                f"{active_agents} | {trace[:80]} | {inserted} |"
            )
        return "\n".join(lines)

    def _render_task_audit(self, run_state: RunState, snapshots: list[IterationSnapshot]) -> str:
        profile = self._task_audit_registry.resolve_for_run(run_state.metadata)
        if profile is None:
            return "## 任务专项审计\n\n本 Run 未启用 task-specific audit。"
        audit_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.step_kind == StepKind.RUN_TASK_AUDIT.value
            and snapshot.metadata.get("audit_profile") == profile.profile_id
        ]
        if not audit_snapshots:
            return (
                "## 任务专项审计\n\n"
                "| 项目 | 值 |\n"
                "|------|------|\n"
                f"| Profile | {profile.profile_id} |\n"
                "| 状态 | 尚未生成专项审计结果 |"
            )
        latest = audit_snapshots[-1]
        artifact_path = latest.metadata.get("audit_artifact_path")
        if artifact_path is None:
            return (
                "## 任务专项审计\n\n"
                "| 项目 | 值 |\n"
                "|------|------|\n"
                f"| Profile | {profile.profile_id} |\n"
                "| 状态 | 未找到专项审计 artifact |"
            )
        result = profile.parse_result(artifact_path)
        if result is None:
            return (
                "## 任务专项审计\n\n"
                "| 项目 | 值 |\n"
                "|------|------|\n"
                f"| Profile | {profile.profile_id} |\n"
                "| 状态 | 无法解析专项审计 artifact |"
            )
        return profile.render_doc_section(result)

    def _render_lessons_learned(
        self,
        run_state: RunState,
        snapshots: list[IterationSnapshot],
        reviews: list[IterationReview],
    ) -> str:
        project_knowledge: list[str] = []
        failure_lessons: list[str] = []
        effective_strategies: list[str] = []

        for snapshot in snapshots:
            if snapshot.command and snapshot.command not in project_knowledge:
                project_knowledge.append(f"验证命令基线：`{snapshot.command}`")

        for review in reviews:
            if review.outcome in {IterationOutcome.BLOCKED, IterationOutcome.REGRESSED}:
                lesson = review.root_cause.strip()
                if lesson and lesson not in failure_lessons:
                    failure_lessons.append(lesson)
            if review.outcome == IterationOutcome.IMPROVED:
                for action in review.useful_actions:
                    if action not in effective_strategies:
                        effective_strategies.append(action)

        if run_state.spawned_agents and "可以使用子 Agent 并行收集信息" not in effective_strategies:
            effective_strategies.append("可以使用子 Agent 并行收集信息")
        if not project_knowledge:
            project_knowledge.append("当前 Run 未提炼出稳定的项目知识")
        if not failure_lessons:
            failure_lessons.append("本轮未出现可归纳的重复失败模式")
        if not effective_strategies:
            effective_strategies.append("先分析再修改，并在修改后立即验证")

        lines = [
            "## 经验教训",
            "",
            "### 项目知识",
            "",
            *[f"- {item}" for item in project_knowledge],
            "",
            "### 失败教训",
            "",
            *[f"- {item}" for item in failure_lessons],
            "",
            "### 有效策略",
            "",
            *[f"- {item}" for item in effective_strategies],
        ]
        return "\n".join(lines)

    def _generic_agent_issues(self, run_state: RunState) -> list[str]:
        issues: list[str] = []
        for agent in run_state.spawned_agents:
            if agent.is_stale:
                issues.append(f"{agent.agent_id} 的结果可能过期，需要在最新工作区上重新验证。")
            elif agent.success is False:
                reason = agent.termination_reason or "unknown failure"
                issues.append(f"{agent.agent_id} 执行失败：{reason}")
        return issues

    def _terminal_reason(self, events: list[HarnessEvent]) -> str:
        for event in reversed(events):
            if event.event_type in {"run_completed", "run_failed", "run_timed_out"}:
                return event.message or event.event_type
        return "unknown"

    def _parse_time(self, value: str) -> datetime:
        return datetime.fromisoformat(value)

    def _format_duration(self, started_at: datetime, ended_at: datetime) -> str:
        seconds = max(0, int((ended_at - started_at).total_seconds()))
        minutes, remaining = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {remaining}s"
        if minutes > 0:
            return f"{minutes}m {remaining}s"
        return f"{remaining}s"

    def _count_kind(self, spans: list[TraceSpan], kind: str) -> int:
        return sum(1 for span in spans if span.kind == kind)

    def _children_map(self, spans: list[TraceSpan]) -> dict[str, list[TraceSpan]]:
        children: dict[str, list[TraceSpan]] = {}
        for span in spans:
            if span.parent_span_id is None:
                continue
            children.setdefault(span.parent_span_id, []).append(span)
        return children

    def _trace_lines(
        self,
        span: TraceSpan,
        children_map: dict[str, list[TraceSpan]],
        *,
        indent: int,
    ) -> list[str]:
        prefix = "  " * indent + "- "
        duration = self._format_span_duration(span.duration_ms)
        summary = f" {span.summary[:60]}" if span.summary else ""
        lines = [f"{prefix}`{span.kind}:{span.name}` [{span.status}] ({duration}){summary}"]
        for child in children_map.get(span.span_id, []):
            lines.extend(self._trace_lines(child, children_map, indent=indent + 1))
        return lines

    def _format_span_duration(self, duration_ms: int | None) -> str:
        if duration_ms is None:
            return "-"
        if duration_ms >= 1000:
            return f"{duration_ms / 1000:.1f}s"
        return f"{duration_ms}ms"

    def _decision_trace_summary(self, event: HarnessEvent) -> str:
        if "trace_outline" in event.data:
            return event.data["trace_outline"]
        span_count = event.data.get("trace_span_count")
        if span_count is None:
            return "-"
        tool_count = event.data.get("trace_tool_count", "0")
        agent_count = event.data.get("trace_agent_count", "0")
        return f"spans={span_count}, tools={tool_count}, agents={agent_count}"

    def _fmt_ms_value(self, value: str | None) -> str:
        if value is None or not value.strip():
            return "-"
        try:
            duration_ms = int(value)
        except ValueError:
            return value
        return self._format_span_duration(duration_ms)

    def _fmt_turn_series(self, value: str | None) -> str:
        if value is None or not value.strip():
            return "-"
        parts = [item for item in value.split(",") if item.strip()]
        if not parts:
            return "-"
        return ", ".join(self._fmt_ms_value(item) for item in parts[:3])

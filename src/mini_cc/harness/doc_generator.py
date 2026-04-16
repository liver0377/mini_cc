from __future__ import annotations

from datetime import datetime

from mini_cc.harness.checkpoint import CheckpointStore
from mini_cc.harness.events import HarnessEvent
from mini_cc.harness.iteration import IterationOutcome, IterationReview, IterationSnapshot
from mini_cc.harness.models import RunState, StepKind, StepStatus
from mini_cc.harness.task_audit import TaskAuditRegistry


class RunDocGenerator:
    def __init__(self, task_audit_registry: TaskAuditRegistry | None = None) -> None:
        self._task_audit_registry = task_audit_registry or TaskAuditRegistry()

    def generate(self, run_state: RunState, store: CheckpointStore) -> str:
        events = store.load_events(run_state.run_id)
        snapshots = store.load_iteration_snapshots(run_state.run_id)
        reviews = store.load_iteration_reviews(run_state.run_id)
        review_map = {review.step_id: review for review in reviews}

        sections = [
            self._render_basic_info(run_state, events),
            self._render_step_timeline(run_state, review_map),
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
            f"| 创建时间 | {run_state.created_at} |",
            f"| 结束时间 | {run_state.updated_at} |",
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
            outcome = review_map.get(step.id).outcome.value if step.id in review_map else "-"
            summary = (step.summary or step.goal).replace("\n", " ")
            lines.append(
                f"| {index} | {step.id} | {step.kind.value} | {step.status.value} | {outcome} | {summary[:80]} |"
            )
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
            lines.append(
                f"| {review.step_id} | {review.score.total} | {review.outcome.value} | {root_cause[:80]} |"
            )
        return "\n".join(lines)

    def _render_agent_summary(self, run_state: RunState) -> str:
        readonly_agents = int(run_state.metadata.get("agents_created_readonly", "0"))
        write_agents = int(run_state.metadata.get("agents_created_write", "0"))
        successful_agents = int(run_state.metadata.get("agents_succeeded", "0"))
        failed_agents = int(run_state.metadata.get("agents_failed", "0"))
        peak_active = int(run_state.metadata.get("agent_peak_active", str(run_state.active_agent_count)))

        lines = [
            "## 子 Agent 活动摘要",
            "",
            "| 指标 | 值 |",
            "|------|------|",
            f"| 总创建数 | {len(run_state.spawned_agents)} |",
            f"| Readonly / Write | {readonly_agents} / {write_agents} |",
            f"| 成功 / 失败 | {successful_agents} / {failed_agents} |",
            f"| 活跃 Agent 峰值 | {peak_active} |",
        ]
        if not run_state.spawned_agents:
            lines.extend(["", "本 Run 未使用子 Agent。"])
            return "\n".join(lines)

        lines.extend(
            [
                "",
                "| Agent | 类型 | 来源 Step | Scope | 结果 | 终止原因 |",
                "|-------|------|-----------|-------|------|----------|",
            ]
        )
        for agent in run_state.spawned_agents:
            result = "运行中"
            if agent.success is True:
                result = "成功"
            elif agent.success is False:
                result = "失败"
            reason = agent.termination_reason or "-"
            lines.append(
                f"| {agent.agent_id} | {'readonly' if agent.readonly else 'write'} | "
                f"{agent.source_step_id or '-'} | {', '.join(agent.scope_paths) or '.'} | {result} | {reason} |"
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
            f"| REPLAN 次数 | {run_state.replan_count} | 3 |",
            f"| 活跃 Agent 峰值 | {peak_active} | {run_state.budget.max_active_agents * 2} |",
        ]
        return "\n".join(lines)

    def _render_quality_assessment(self, run_state: RunState, reviews: list[IterationReview]) -> str:
        final_review = reviews[-1] if reviews else None
        unresolved = final_review.next_constraints if final_review is not None else []
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
        return "\n".join(lines)

    def _render_decisions(self, events: list[HarnessEvent]) -> str:
        lines = [
            "## 关键决策记录",
            "",
            "| Step | 决策 | 原因 | Active Agents | 自动插入 |",
            "|------|------|------|---------------|----------|",
        ]
        decision_events = [
            event
            for event in events
            if event.event_type in {"run_failed", "run_timed_out", "step_completed", "run_completed", "run_resumed"}
        ]
        if not decision_events:
            lines.append("| - | - | - | - | - |")
            return "\n".join(lines)
        for event in decision_events[-12:]:
            decision = event.data.get("decision", event.event_type)
            reason = event.data.get("decision_reason", event.message).replace("\n", " ")
            active_agents = event.data.get("active_agents", "-")
            inserted = event.data.get("inserted_steps", "-") or "-"
            lines.append(
                f"| {event.step_id or event.event_type} | {decision} | {reason[:60]} | {active_agents} | {inserted} |"
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

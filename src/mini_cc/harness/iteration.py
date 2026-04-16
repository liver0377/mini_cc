from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from mini_cc.harness.models import RunState, Step, StepKind, StepResult, StepStatus
from mini_cc.harness.task_audit import TaskAuditRegistry


class IterationOutcome(StrEnum):
    IMPROVED = "improved"
    STALLED = "stalled"
    REGRESSED = "regressed"
    BLOCKED = "blocked"


class IterationScore(BaseModel):
    total: int
    success_signal: int = 0
    progress_signal: int = 0
    verification_signal: int = 0
    artifact_signal: int = 0
    comparison_signal: int = 0
    penalty: int = 0


class IterationSnapshot(BaseModel):
    run_id: str
    step_id: str
    step_kind: str
    success: bool
    summary: str
    error: str | None = None
    progress_made: bool = False
    command: str | None = None
    test_passed: int = 0
    test_failed: int = 0
    test_errors: int = 0
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)


class IterationReview(BaseModel):
    run_id: str
    step_id: str
    outcome: IterationOutcome
    score: IterationScore
    root_cause: str
    useful_actions: list[str] = Field(default_factory=list)
    wasted_actions: list[str] = Field(default_factory=list)
    next_constraints: list[str] = Field(default_factory=list)
    recommended_step_kind: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class IterationOptimizer:
    _PROMPT_STEP_KINDS = {
        StepKind.BOOTSTRAP_PROJECT,
        StepKind.ANALYZE_REPO,
        StepKind.MAKE_PLAN,
        StepKind.EDIT_CODE,
        StepKind.SUMMARIZE_PROGRESS,
        StepKind.FINALIZE,
        StepKind.SPAWN_READONLY_AGENT,
    }

    def __init__(self, task_audit_registry: TaskAuditRegistry | None = None) -> None:
        self._task_audit_registry = task_audit_registry or TaskAuditRegistry()

    def capture(
        self,
        run_state: RunState,
        step: Step,
        result: StepResult,
        artifact_paths: dict[str, str],
    ) -> IterationSnapshot:
        test_passed = self._extract_count(result.summary, "passed")
        test_failed = self._extract_count(result.summary, "failed")
        test_errors = self._extract_count(result.summary, "error") + self._extract_count(result.summary, "errors")
        command = self._string_metadata(result.metadata, "command")
        metadata = {key: value for key, value in result.metadata.items() if isinstance(value, str)}
        agent_issue = run_state.metadata.get("agent_latest_issue")
        if agent_issue:
            metadata["agent_latest_issue"] = agent_issue
        for key in ("agents_failed", "agents_stale", "agents_cancelled"):
            value = run_state.metadata.get(key)
            if value is not None:
                metadata[key] = value
        audit_result = self._task_audit_registry.parse_result(run_state.metadata, artifact_paths)
        if audit_result is not None:
            profile = self._task_audit_registry.get(audit_result.profile_id)
            if profile is not None:
                metadata.update(profile.snapshot_metadata(audit_result))
        return IterationSnapshot(
            run_id=run_state.run_id,
            step_id=step.id,
            step_kind=step.kind.value,
            success=result.success,
            summary=result.summary,
            error=result.error,
            progress_made=result.progress_made,
            command=command,
            test_passed=test_passed,
            test_failed=test_failed,
            test_errors=test_errors,
            artifact_paths=artifact_paths,
            metadata=metadata,
        )

    def review(
        self,
        current: IterationSnapshot,
        previous: IterationSnapshot | None,
    ) -> IterationReview:
        score = self._score(current, previous)
        outcome = self._classify(current, previous, score)
        root_cause = self._root_cause(current, previous, outcome)
        useful_actions = self._useful_actions(current)
        wasted_actions = self._wasted_actions(current, outcome)
        next_constraints = self._next_constraints(current, outcome)
        recommended_step_kind = self._recommended_step_kind(current, outcome)
        audit_review_metadata, audit_root_cause, audit_constraints = self._task_audit_review(current, previous)
        if audit_root_cause is not None:
            root_cause = audit_root_cause
        if audit_constraints:
            next_constraints.extend(item for item in audit_constraints if item not in next_constraints)
        return IterationReview(
            run_id=current.run_id,
            step_id=current.step_id,
            outcome=outcome,
            score=score,
            root_cause=root_cause,
            useful_actions=useful_actions,
            wasted_actions=wasted_actions,
            next_constraints=next_constraints,
            recommended_step_kind=recommended_step_kind,
            metadata={"step_kind": current.step_kind, **audit_review_metadata},
        )

    def apply_review(
        self,
        run_state: RunState,
        step: Step,
        result: StepResult,
        review: IterationReview,
    ) -> list[Step]:
        generated: list[Step] = []
        if (
            step.kind == StepKind.EDIT_CODE
            and result.success
            and not self._has_pending_kind(run_state, StepKind.RUN_TESTS)
        ):
            command = self._default_test_command(run_state, step)
            generated.append(
                Step(
                    kind=StepKind.RUN_TESTS,
                    title="Verify Changes",
                    goal=self._goal_with_constraints(
                        "Run the project's verification command and capture the result.",
                        review.next_constraints,
                    ),
                    inputs={"command": command},
                )
            )

        if step.kind == StepKind.RUN_TESTS and not result.success:
            command = self._inspect_command(result, run_state)
            generated.append(
                Step(
                    kind=StepKind.INSPECT_FAILURES,
                    title="Inspect Failures",
                    goal=self._goal_with_constraints(
                        "Collect a more actionable failure trace before replanning.",
                        review.next_constraints,
                    ),
                    inputs={"command": command},
                )
            )
        if (
            step.kind == StepKind.RUN_TESTS
            and result.success
            and run_state.metadata.get("audit_profile")
            and not self._has_pending_kind(run_state, StepKind.RUN_TASK_AUDIT)
        ):
            audit_command = self._default_task_audit_command(run_state, step)
            audit_step = self._task_audit_registry.build_audit_step(run_state.metadata, audit_command)
            if audit_step is not None:
                generated.append(audit_step)
        if (
            step.kind == StepKind.RUN_TASK_AUDIT
            and result.success
            and not self._has_pending_kind(run_state, StepKind.FINALIZE)
            and not self._has_blockers(result)
        ):
            generated.append(
                Step(
                    kind=StepKind.FINALIZE,
                    title="Finalize",
                    goal="Summarize the run outcome and produce final documentation.",
                )
            )
        return generated

    def format_journal_entry(
        self,
        snapshot: IterationSnapshot,
        review: IterationReview,
        generated_steps: list[Step],
    ) -> str:
        lines = [
            f"## {snapshot.step_id} `{snapshot.step_kind}`",
            f"- Outcome: `{review.outcome.value}`",
            f"- Success: `{str(snapshot.success).lower()}`",
            f"- Summary: {snapshot.summary or 'n/a'}",
            f"- Root cause: {review.root_cause}",
        ]
        if snapshot.error:
            lines.append(f"- Error: {snapshot.error}")
        if review.next_constraints:
            lines.append(f"- Constraints: {'; '.join(review.next_constraints)}")
        if generated_steps:
            lines.append(f"- Next steps: {', '.join(step.title for step in generated_steps)}")
        if snapshot.command:
            lines.append(f"- Command: `{snapshot.command}`")
        return "\n".join(lines) + "\n\n"

    def apply_constraints_to_steps(self, steps: list[Step], review: IterationReview) -> list[Step]:
        if not review.next_constraints:
            return steps

        constrained_steps: list[Step] = []
        for step in steps:
            updated = step.model_copy(deep=True)
            updated.goal = self._goal_with_constraints(updated.goal, review.next_constraints)
            if self._supports_prompt(updated):
                prompt = updated.inputs.get("prompt")
                prompt_text = prompt if isinstance(prompt, str) and prompt.strip() else updated.goal
                updated.inputs["prompt"] = self._prompt_with_constraints(prompt_text, review.next_constraints)
            constrained_steps.append(updated)
        return constrained_steps

    def _score(
        self,
        current: IterationSnapshot,
        previous: IterationSnapshot | None,
    ) -> IterationScore:
        success_signal = 3 if current.success else 0
        progress_signal = 2 if current.progress_made else 0
        verification_signal = 0
        if current.step_kind == StepKind.RUN_TESTS.value:
            verification_signal += current.test_passed
            verification_signal -= current.test_failed + current.test_errors
            if current.success:
                verification_signal += 2

        artifact_signal = 1 if current.artifact_paths else 0
        agent_issue_penalty = 0
        if self._string_metadata(current.metadata, "agent_latest_issue"):
            agent_issue_penalty += 1
        agent_issue_penalty += int(self._string_metadata(current.metadata, "agents_failed") or "0")
        agent_issue_penalty += int(self._string_metadata(current.metadata, "agents_stale") or "0")
        comparison_signal = 0
        if previous is not None:
            if current.success and not previous.success:
                comparison_signal += 2
            if current.test_failed < previous.test_failed:
                comparison_signal += previous.test_failed - current.test_failed
            if current.test_failed > previous.test_failed:
                comparison_signal -= current.test_failed - previous.test_failed
            if current.error and previous.error and current.error == previous.error:
                comparison_signal -= 1
            previous_issue = self._string_metadata(previous.metadata, "agent_latest_issue")
            current_issue = self._string_metadata(current.metadata, "agent_latest_issue")
            if current_issue and previous_issue == current_issue:
                comparison_signal -= 1

        penalty = 2 if current.error else 0
        penalty += agent_issue_penalty
        total = success_signal + progress_signal + verification_signal + artifact_signal + comparison_signal - penalty
        return IterationScore(
            total=total,
            success_signal=success_signal,
            progress_signal=progress_signal,
            verification_signal=verification_signal,
            artifact_signal=artifact_signal,
            comparison_signal=comparison_signal,
            penalty=penalty,
        )

    def _classify(
        self,
        current: IterationSnapshot,
        previous: IterationSnapshot | None,
        score: IterationScore,
    ) -> IterationOutcome:
        if self._string_metadata(current.metadata, "agent_latest_issue") and not current.progress_made:
            if previous is not None and self._string_metadata(previous.metadata, "agent_latest_issue") == self._string_metadata(
                current.metadata, "agent_latest_issue"
            ):
                return IterationOutcome.BLOCKED
            return IterationOutcome.REGRESSED
        if not current.success and current.error and previous is not None and previous.error == current.error:
            return IterationOutcome.BLOCKED
        if previous is None:
            return IterationOutcome.IMPROVED if current.success or current.progress_made else IterationOutcome.STALLED
        if score.total > 0:
            return IterationOutcome.IMPROVED
        if not current.success and not current.progress_made:
            return IterationOutcome.REGRESSED
        if current.success and not current.progress_made:
            return IterationOutcome.STALLED
        if score.total < 0:
            return IterationOutcome.REGRESSED
        return IterationOutcome.STALLED

    def _root_cause(
        self,
        current: IterationSnapshot,
        previous: IterationSnapshot | None,
        outcome: IterationOutcome,
    ) -> str:
        agent_issue = self._string_metadata(current.metadata, "agent_latest_issue")
        if agent_issue:
            return f"Sub-agent issue: {agent_issue}"
        if current.error:
            if previous is not None and previous.error == current.error:
                return f"Repeated failure: {current.error}"
            return current.error
        if outcome == IterationOutcome.STALLED:
            return "Step completed without a strong progress signal"
        if current.step_kind == StepKind.RUN_TESTS.value and current.test_failed > 0:
            return f"{current.test_failed} tests are still failing"
        return "Latest step produced a usable result"

    def _useful_actions(self, current: IterationSnapshot) -> list[str]:
        actions: list[str] = []
        if current.success:
            actions.append("Preserve the latest successful change set")
        if current.artifact_paths:
            actions.append("Use saved artifacts when planning the next step")
        if current.step_kind == StepKind.RUN_TESTS.value and current.command:
            actions.append("Keep the verification command stable between iterations")
        if self._string_metadata(current.metadata, "agent_latest_issue") is None:
            actions.append("Reuse successful sub-agent findings when they remain fresh")
        return actions

    def _wasted_actions(self, current: IterationSnapshot, outcome: IterationOutcome) -> list[str]:
        if outcome in {IterationOutcome.BLOCKED, IterationOutcome.REGRESSED}:
            return ["Do not repeat the same step without new diagnostics"]
        if outcome == IterationOutcome.STALLED:
            return ["Avoid summary-only steps without a follow-up verification action"]
        return []

    def _next_constraints(self, current: IterationSnapshot, outcome: IterationOutcome) -> list[str]:
        constraints: list[str] = []
        agent_issue = self._string_metadata(current.metadata, "agent_latest_issue")
        if agent_issue:
            constraints.append(f"Resolve sub-agent issue before trusting delegated findings: {agent_issue}")
        stale_agents = int(self._string_metadata(current.metadata, "agents_stale") or "0")
        if stale_agents > 0:
            constraints.append(f"Revalidate stale sub-agent outputs ({stale_agents}) against the latest workspace")
        if current.step_kind == StepKind.RUN_TESTS.value:
            if current.test_failed > 0:
                constraints.append(f"Reduce failing tests below {current.test_failed} before finalizing")
            if current.command:
                constraints.append(f"Keep using `{current.command}` as the verification baseline")
        if current.step_kind == StepKind.RUN_TASK_AUDIT.value:
            blocker = self._string_metadata(current.metadata, "audit_blockers")
            next_focus = self._string_metadata(current.metadata, "audit_next_focus")
            if blocker:
                constraints.append(f"Resolve task audit blocker: {blocker}")
            if next_focus:
                constraints.append(f"Focus next work on: {next_focus}")
        if current.error:
            constraints.append(f"Address this error directly: {current.error}")
        if outcome == IterationOutcome.STALLED:
            constraints.append("The next step must end with an observable validation signal")
        if outcome == IterationOutcome.BLOCKED:
            constraints.append("Collect new diagnostics before retrying the same fix path")
        return constraints

    def _recommended_step_kind(
        self,
        current: IterationSnapshot,
        outcome: IterationOutcome,
    ) -> str | None:
        if self._string_metadata(current.metadata, "agent_latest_issue"):
            return StepKind.MAKE_PLAN.value
        if current.step_kind == StepKind.RUN_TESTS.value and not current.success:
            return StepKind.INSPECT_FAILURES.value
        if current.step_kind == StepKind.RUN_TASK_AUDIT.value and not current.success:
            return StepKind.MAKE_PLAN.value
        if outcome in {IterationOutcome.BLOCKED, IterationOutcome.STALLED, IterationOutcome.REGRESSED}:
            return StepKind.MAKE_PLAN.value
        if current.step_kind == StepKind.EDIT_CODE.value and current.success:
            return StepKind.RUN_TESTS.value
        return None

    def _goal_with_constraints(self, goal: str, constraints: list[str]) -> str:
        if not constraints or "Constraints:" in goal:
            return goal
        return f"{goal} Constraints: {'; '.join(constraints)}."

    def _prompt_with_constraints(self, prompt: str, constraints: list[str]) -> str:
        if not constraints or "Constraints:" in prompt:
            return prompt
        return f"{prompt}\n\nConstraints:\n- " + "\n- ".join(constraints)

    def _default_test_command(self, run_state: RunState, step: Step) -> str:
        metadata_command = run_state.metadata.get("test_command")
        if metadata_command:
            return metadata_command
        explicit = step.inputs.get("test_command")
        if isinstance(explicit, str) and explicit.strip():
            return explicit
        return "uv run pytest"

    def _inspect_command(self, result: StepResult, run_state: RunState) -> str:
        command = (
            self._string_metadata(result.metadata, "command")
            or run_state.metadata.get("test_command")
            or "uv run pytest"
        )
        if "pytest" in command and "-x" not in command:
            return f"{command} -x -vv"
        return command

    def _default_task_audit_command(self, run_state: RunState, step: Step) -> str:
        metadata_command = run_state.metadata.get("task_audit_command")
        if metadata_command:
            return metadata_command
        explicit = step.inputs.get("task_audit_command")
        if isinstance(explicit, str) and explicit.strip():
            return explicit
        profile_id = run_state.metadata.get("audit_profile", "task")
        script_path = Path(f"scripts/task_audit/{profile_id}.py")
        command = (
            f"uv run python {script_path}"
            if script_path.is_file()
            else f"uv run python scripts/task_audit/{profile_id}.py"
        )
        prev_path = self._find_prev_audit_path(run_state)
        if prev_path is not None:
            command += f" {prev_path}"
        return command

    def _find_prev_audit_path(self, run_state: RunState) -> str | None:
        for s in reversed(run_state.steps):
            if s.kind == StepKind.RUN_TASK_AUDIT and s.status == StepStatus.SUCCEEDED:
                for path in s.artifacts.values():
                    if Path(path).is_file():
                        return path
        return None

    def _has_pending_kind(self, run_state: RunState, kind: StepKind) -> bool:
        return any(
            step.kind == kind and step.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}
            for step in run_state.steps
        )

    def _has_blockers(self, result: StepResult) -> bool:
        blockers = result.metadata.get("audit_blockers", "")
        if blockers.strip():
            return True
        if result.metadata.get("audit_profile") and not result.metadata.get("audit_summary"):
            return True
        return False

    def _supports_prompt(self, step: Step) -> bool:
        return step.kind in self._PROMPT_STEP_KINDS or "prompt" in step.inputs

    def _string_metadata(self, metadata: dict[str, str], key: str) -> str | None:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
        return None

    def _task_audit_review(
        self,
        current: IterationSnapshot,
        previous: IterationSnapshot | None,
    ) -> tuple[dict[str, str], str | None, list[str]]:
        profile_id = self._string_metadata(current.metadata, "audit_profile")
        if current.step_kind != StepKind.RUN_TASK_AUDIT.value or profile_id is None:
            return {}, None, []
        profile = self._task_audit_registry.get(profile_id)
        if profile is None:
            return {}, None, []
        artifact_path = self._string_metadata(current.metadata, "audit_artifact_path")
        if artifact_path is None:
            return {}, None, []
        current_result = profile.parse_result(artifact_path)
        if current_result is None:
            return {}, None, []
        previous_result = None
        previous_path = (
            self._string_metadata(previous.metadata, "audit_artifact_path") if previous is not None else None
        )
        if previous_path is not None:
            previous_result = profile.parse_result(previous_path)
        return profile.review_metadata(current_result, previous_result)

    def _extract_count(self, text: str, label: str) -> int:
        match = re.search(rf"(\d+)\s+{label}\b", text, re.IGNORECASE)
        if match is None:
            return 0
        return int(match.group(1))

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from mini_cc.agent import AgentManager
from mini_cc.context.engine_context import EngineContext
from mini_cc.harness.models import AgentBudget, RunState, Step, StepKind, StepResult
from mini_cc.models import Event, Message, QueryState, Role, TextDelta, ToolResultEvent
from mini_cc.tools.bash import Bash

StepHandler = Callable[[Step, RunState], Awaitable[StepResult]]
QueryEventSink = Callable[[Event, Step, RunState], Awaitable[None] | None]


class StepRunner:
    def __init__(
        self,
        *,
        engine_ctx: EngineContext | None = None,
        handlers: dict[StepKind, StepHandler] | None = None,
        bash_tool: Bash | None = None,
        query_event_sink: QueryEventSink | None = None,
    ) -> None:
        self._engine_ctx = engine_ctx
        self._handlers = handlers or {}
        self._bash_tool = bash_tool or Bash()
        self._query_event_sink = query_event_sink

    def register_handler(self, kind: StepKind, handler: StepHandler) -> None:
        self._handlers[kind] = handler

    async def run_step(self, step: Step, run_state: RunState) -> StepResult:
        if self._engine_ctx is not None:
            self._engine_ctx.current_run_id = run_state.run_id
        handler = self._handlers.get(step.kind)
        if handler is not None:
            return await handler(step, run_state)

        if step.kind in {
            StepKind.ANALYZE_REPO,
            StepKind.MAKE_PLAN,
            StepKind.EDIT_CODE,
            StepKind.SUMMARIZE_PROGRESS,
            StepKind.FINALIZE,
        }:
            return await self._run_query_step(step, run_state)
        if step.kind in {StepKind.RUN_TESTS, StepKind.RUN_TASK_AUDIT, StepKind.INSPECT_FAILURES}:
            return await self._run_bash_step(step)
        if step.kind == StepKind.CHECKPOINT:
            return StepResult(success=True, summary=f"Checkpoint requested by step {step.id}", progress_made=False)
        if step.kind == StepKind.SPAWN_READONLY_AGENT:
            return await self._spawn_readonly_agent(step, run_state)

        return StepResult(success=False, summary="", retryable=False, error=f"Unsupported step kind: {step.kind}")

    async def _run_query_step(self, step: Step, run_state: RunState) -> StepResult:
        if self._engine_ctx is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="EngineContext is required for query-backed steps",
            )

        self._inject_agent_budget(run_state)
        prompt = str(step.inputs.get("prompt", step.goal))
        mode = "build" if step.kind == StepKind.EDIT_CODE else "plan"
        prev_mode = self._engine_ctx.mode
        self._engine_ctx.mode = mode
        state = self._prepare_query_state(run_state.latest_query_state, mode)
        text_parts: list[str] = []
        tool_outputs: list[str] = []

        try:
            async for event in self._engine_ctx.engine.submit_message(prompt, state):
                await self._emit_query_event(event, step, run_state)
                if isinstance(event, TextDelta):
                    text_parts.append(event.content)
                elif isinstance(event, ToolResultEvent):
                    tool_outputs.append(event.output)
        except Exception as err:
            self._engine_ctx.mode = prev_mode
            return StepResult(
                success=False,
                summary="",
                retryable=True,
                error=str(err),
                query_state=state,
            )

        self._engine_ctx.mode = prev_mode
        summary = "".join(text_parts).strip()
        if not summary and tool_outputs:
            summary = "\n\n".join(tool_outputs[:3]).strip()
        progress_made = bool(summary or tool_outputs or state.turn_count > 0)
        metadata = self._read_back_budget_metadata()
        return StepResult(
            success=True,
            summary=summary or f"Completed step {step.id}",
            progress_made=progress_made,
            query_state=state,
            metadata=metadata,
        )

    async def _run_bash_step(self, step: Step) -> StepResult:
        command_value = step.inputs.get("command")
        if not isinstance(command_value, str) or not command_value.strip():
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="Bash-backed step requires a non-empty command input",
            )

        timeout_value = step.inputs.get("timeout")
        timeout = timeout_value if isinstance(timeout_value, int) else 120000
        result = await asyncio.to_thread(self._bash_tool.execute, command=command_value, timeout=timeout)
        output = result.output or result.error or ""
        artifact_name_value = step.inputs.get("artifact_name")
        if isinstance(artifact_name_value, str) and artifact_name_value.strip():
            artifact_name = artifact_name_value.strip()
        else:
            artifact_name = f"{step.id or step.kind.value}.txt"
        artifact_key = "task_audit" if step.kind == StepKind.RUN_TASK_AUDIT else artifact_name
        return StepResult(
            success=result.success,
            summary=output[:1000].strip(),
            artifacts={artifact_key: output},
            retryable=result.success is False,
            error=result.error,
            progress_made=bool(output.strip()),
            metadata={
                "command": command_value,
                "timeout_ms": str(timeout),
                "artifact_name": artifact_name,
                "audit_profile": str(step.inputs.get("profile", "")),
            },
        )

    async def _spawn_readonly_agent(self, step: Step, run_state: RunState) -> StepResult:
        if self._engine_ctx is None or self._engine_ctx.agent_manager is None:
            return StepResult(
                success=False,
                summary="",
                retryable=False,
                error="AgentManager is required for readonly agent steps",
            )

        self._inject_agent_budget(run_state)
        prompt = str(step.inputs.get("prompt", step.goal))
        manager: AgentManager = self._engine_ctx.agent_manager
        agent = await manager.create_agent(
            prompt=prompt,
            readonly=True,
            fork=bool(step.inputs.get("fork", False)),
            parent_state=run_state.latest_query_state,
            mode="plan",
        )
        asyncio.create_task(agent.run_background(prompt))
        metadata: dict[str, str] = {"agent_id": agent.config.agent_id}
        metadata.update(self._read_back_budget_metadata())
        return StepResult(
            success=True,
            summary=f"Readonly agent {agent.config.agent_id} started for step {step.id}",
            progress_made=True,
            metadata=metadata,
        )

    async def _emit_query_event(self, event: Event, step: Step, run_state: RunState) -> None:
        if self._query_event_sink is None:
            return
        result = self._query_event_sink(event, step, run_state)
        if inspect.isawaitable(result):
            await result

    def _prepare_query_state(self, state: QueryState | None, mode: str) -> QueryState:
        assert self._engine_ctx is not None
        run_id = self._engine_ctx.current_run_id
        content = self._engine_ctx.prompt_builder.build(self._engine_ctx.env_info, mode=mode, run_id=run_id)
        if state is None:
            return QueryState(messages=[Message(role=Role.SYSTEM, content=content)])

        next_state = state.model_copy(deep=True)
        if next_state.messages and next_state.messages[0].role == Role.SYSTEM:
            next_state.messages[0] = Message(role=Role.SYSTEM, content=content)
        else:
            next_state.messages.insert(0, Message(role=Role.SYSTEM, content=content))
        return next_state

    def _inject_agent_budget(self, run_state: RunState) -> None:
        if self._engine_ctx is None:
            return
        if run_state.agent_budget is not None:
            configured = run_state.agent_budget.model_copy(deep=True)
        else:
            configured = AgentBudget(
                max_readonly=run_state.budget.max_active_agents,
                max_write=1,
                remaining_readonly=run_state.budget.max_active_agents,
                remaining_write=1,
            )
        active_readonly = run_state.active_readonly_agent_count
        active_write = run_state.active_write_agent_count
        self._engine_ctx.agent_budget = AgentBudget(
            max_readonly=configured.max_readonly,
            max_write=1,
            remaining_readonly=max(0, configured.max_readonly - active_readonly),
            remaining_write=max(0, 1 - active_write),
        )

    def _read_back_budget_metadata(self) -> dict[str, str]:
        if self._engine_ctx is None or self._engine_ctx.agent_budget is None:
            return {}
        budget: AgentBudget = self._engine_ctx.agent_budget  # type: ignore[assignment]
        return {
            "agents_remaining_ro": str(budget.remaining_readonly),
            "agents_remaining_w": str(budget.remaining_write),
        }

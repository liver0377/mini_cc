from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

from mini_cc.context.system_prompt import EnvInfo, SystemPromptBuilder
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.models import (
    AgentCompletionEvent,
    AgentConfig,
    AgentStatus,
    Event,
    Message,
    QueryState,
    Role,
    TaskType,
    generate_agent_id,
)
from mini_cc.runtime.agents.bus import AgentEventBus, AgentLifecycleEvent
from mini_cc.runtime.agents.snapshot import SnapshotService
from mini_cc.runtime.agents.sub_agent import SubAgent, build_workspace_notice
from mini_cc.runtime.execution.executor import StreamingToolExecutor
from mini_cc.runtime.query.engine import QueryEngine, StreamFn
from mini_cc.task.service import TaskService
from mini_cc.tools import create_default_registry, create_readonly_registry


class AgentManager:
    def __init__(
        self,
        *,
        project_root: Path,
        stream_fn: StreamFn,
        task_service: TaskService,
        completion_queue: asyncio.Queue[AgentCompletionEvent],
        agent_event_queue: asyncio.Queue[Event] | None = None,
        prompt_builder: SystemPromptBuilder | None = None,
        env_info: EnvInfo | None = None,
        lifecycle_bus: AgentEventBus | None = None,
    ) -> None:
        self._project_root = project_root
        self._stream_fn = stream_fn
        self._task_service = task_service
        self._completion_queue = completion_queue
        self._agent_event_queue = agent_event_queue
        self._prompt_builder = prompt_builder
        self._env_info = env_info
        self._lifecycle_bus = lifecycle_bus
        self._agents: dict[str, SubAgent] = {}
        self._output_dir = project_root / ".mini_cc" / "tasks"
        self._current_step_id: str | None = None

    @property
    def agents(self) -> dict[str, SubAgent]:
        return dict(self._agents)

    async def create_agent(
        self,
        *,
        prompt: str,
        readonly: bool = False,
        fork: bool = False,
        parent_state: QueryState | None = None,
        mode: str = "build",
        scope_paths: list[str] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        work_item_id: str | None = None,
        role: str | None = None,
    ) -> SubAgent:
        agent_id = generate_agent_id()
        normalized_scopes = self._normalize_scope_paths(scope_paths)
        if not readonly:
            self._assert_write_scope_available(normalized_scopes, agent_id)
        base_version_stamp = self._workspace_version_stamp()

        config = AgentConfig(
            agent_id=agent_id,
            workspace_path=str(self._project_root),
            is_fork=fork,
            is_readonly=readonly,
            parent_agent_id=None,
            scope_paths=normalized_scopes,
            base_version_stamp=base_version_stamp,
        )

        task = await self._task_service.create(
            type=TaskType.LOCAL_AGENT,
            subject=prompt[:80],
            description=prompt,
            agent_id=agent_id,
            prompt=prompt,
            is_fork=fork,
            metadata={
                "scope_paths": normalized_scopes,
                "base_version_stamp": base_version_stamp,
                "readonly": readonly,
                "run_id": run_id or "",
                "step_id": step_id or self._current_step_id or "",
                "work_item_id": work_item_id or "",
                "role": role or "",
            },
        )

        state = self._build_initial_state(config, fork, parent_state, mode, run_id)
        engine, snapshot_svc, interrupt_event = self._build_engine(config)

        agent = SubAgent(
            config=config,
            engine=engine,
            state=state,
            task_id=task.id,
            task_service=self._task_service,
            completion_queue=self._completion_queue,
            output_dir=self._output_dir,
            snapshot_svc=snapshot_svc,
            event_queue=self._agent_event_queue,
            version_provider=self._workspace_version_stamp,
            lifecycle_bus=self._lifecycle_bus,
            interrupt_event=interrupt_event,
        )

        self._agents[agent_id] = agent

        if self._lifecycle_bus is not None:
            self._lifecycle_bus.publish_nowait(
                AgentLifecycleEvent(
                    event_type="created",
                    agent_id=agent_id,
                    source_step_id=step_id or self._current_step_id,
                    readonly=readonly,
                    scope_paths=normalized_scopes,
                )
            )

        return agent

    def get_agent(self, agent_id: str) -> SubAgent | None:
        return self._agents.get(agent_id)

    async def cleanup(self, agent_id: str) -> None:
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            return
        if agent.snapshot_svc is not None:
            agent.snapshot_svc.cleanup()
        output_path = self._output_dir / f"{agent_id}.output"
        output_path.unlink(missing_ok=True)

    def set_current_step(self, step_id: str | None) -> None:
        self._current_step_id = step_id

    def clear_current_step(self) -> None:
        self._current_step_id = None

    def cancel_agents(self, agent_ids: list[str] | None = None) -> list[str]:
        cancelled: list[str] = []
        target_ids = set(agent_ids) if agent_ids is not None else None
        for agent_id, agent in self._agents.items():
            if target_ids is not None and agent_id not in target_ids:
                continue
            if agent.status in {AgentStatus.COMPLETED, AgentStatus.CANCELLED}:
                continue
            agent.cancel()
            cancelled.append(agent_id)
        return cancelled

    def _build_initial_state(
        self,
        config: AgentConfig,
        fork: bool,
        parent_state: QueryState | None,
        mode: str = "build",
        run_id: str | None = None,
    ) -> QueryState:
        if fork and parent_state is not None:
            state = parent_state.model_copy(deep=True)
            notice = build_workspace_notice(config, self._project_root)
            state.messages.append(Message(role=Role.USER, content=notice))
            self._inject_sub_agent_notice(state, config)
            return state

        state = QueryState()
        if self._prompt_builder is not None and self._env_info is not None:
            sub_env = EnvInfo(
                working_directory=config.workspace_path,
                is_git_repo=self._env_info.is_git_repo,
                platform_name=self._env_info.platform_name,
                shell=self._env_info.shell,
                os_version=self._env_info.os_version,
                model_name=self._env_info.model_name,
                model_id=self._env_info.model_id,
            )
            system_content = self._prompt_builder.build_for_sub_agent(
                sub_env,
                mode=mode,
                run_id=run_id,
                context_cwd=str(self._project_root),
            )
            state.messages.insert(0, Message(role=Role.SYSTEM, content=system_content))
        self._inject_sub_agent_notice(state, config)
        return state

    def _inject_sub_agent_notice(self, state: QueryState, config: AgentConfig) -> None:
        if config.is_readonly:
            notice = (
                "\n\n## 子 Agent 身份声明\n"
                "你是一个只读子 Agent，在主项目目录中运行，但只能使用只读工具。\n"
                "- 你只有只读工具（file_read, glob, grep, bash）\n"
                "- 不要修改任何文件\n"
                f"- 你的工作目录: {config.workspace_path}\n"
                f"- 原始项目路径: {self._project_root}\n"
            )
        else:
            notice = (
                "\n\n## 子 Agent 身份声明\n"
                "你是一个子 Agent，正在直接操作主项目的工作目录。\n"
                "你的文件修改会立即生效。\n"
                "- 修改文件前先阅读相关代码，确保理解上下文\n"
                "- 每次只修改必要的最小范围\n"
                "- 修改完成后运行相关测试验证\n"
                f"- 你的工作目录: {config.workspace_path}\n"
                f"- 你的写入范围: {', '.join(config.scope_paths)}\n"
            )
        if state.messages and state.messages[0].role == Role.SYSTEM:
            state.messages[0].content = (state.messages[0].content or "") + notice
        else:
            state.messages.insert(0, Message(role=Role.SYSTEM, content=notice))

    def _build_engine(self, config: AgentConfig) -> tuple[QueryEngine, SnapshotService | None, threading.Event]:
        snapshot: SnapshotService | None = None
        interrupt_event = threading.Event()
        if config.is_readonly:
            registry = create_readonly_registry()
            executor = StreamingToolExecutor(registry, is_interrupted=interrupt_event.is_set)
        else:
            registry = create_default_registry()
            snapshot = SnapshotService(self._project_root, config.agent_id)
            executor = StreamingToolExecutor(
                registry,
                pre_execute_hook=snapshot.on_tool_execute,
                is_interrupted=interrupt_event.is_set,
            )
        tool_use_ctx = ToolUseContext(
            get_schemas=registry.to_api_format,
            execute=executor.run,
            is_interrupted=interrupt_event.is_set,
        )
        engine = QueryEngine(stream_fn=self._stream_fn, tool_use_ctx=tool_use_ctx)
        return engine, snapshot, interrupt_event

    def _normalize_scope_paths(self, scope_paths: list[str] | None) -> list[str]:
        if not scope_paths:
            return ["."]
        normalized: list[str] = []
        for raw_path in scope_paths:
            candidate = Path(raw_path)
            target = candidate if candidate.is_absolute() else (self._project_root / candidate)
            try:
                rel = target.resolve().relative_to(self._project_root.resolve())
                rel_str = str(rel) or "."
            except ValueError:
                rel_str = "."
            if rel_str not in normalized:
                normalized.append(rel_str)
        return normalized or ["."]

    def _assert_write_scope_available(self, scope_paths: list[str], pending_agent_id: str) -> None:
        for agent in self._agents.values():
            if agent.config.agent_id == pending_agent_id:
                continue
            if agent.config.is_readonly:
                continue
            if agent.status not in {AgentStatus.CREATED, AgentStatus.RUNNING, AgentStatus.BACKGROUND_RUNNING}:
                continue
            if self._scopes_overlap(scope_paths, agent.config.scope_paths):
                raise ValueError(
                    f"写 Agent scope 冲突: {scope_paths} 与 {agent.config.agent_id} 的 {agent.config.scope_paths} 重叠"
                )

    def _scopes_overlap(self, left: list[str], right: list[str]) -> bool:
        for left_scope in left:
            for right_scope in right:
                if self._scope_overlap(left_scope, right_scope):
                    return True
        return False

    def _scope_overlap(self, left: str, right: str) -> bool:
        if left == "." or right == ".":
            return True
        left_parts = Path(left).parts
        right_parts = Path(right).parts
        shorter = min(len(left_parts), len(right_parts))
        return left_parts[:shorter] == right_parts[:shorter]

    def _workspace_version_stamp(self) -> str:
        head = self._git_head()
        dirty = self._git_dirty()
        latest_mtime = self._latest_mtime_ns(self._project_root)
        dirty_label = "dirty" if dirty else "clean"
        return f"{head}:{dirty_label}:{latest_mtime}"

    def _git_head(self) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self._project_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "nogit"
        if result.returncode != 0:
            return "nogit"
        return result.stdout.strip() or "nogit"

    def _git_dirty(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self._project_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())

    def _latest_mtime_ns(self, root: Path) -> int:
        latest = 0
        for path in root.rglob("*"):
            if ".git" in path.parts or ".mini_cc" in path.parts:
                continue
            try:
                latest = max(latest, path.stat().st_mtime_ns)
            except OSError:
                continue
        return latest

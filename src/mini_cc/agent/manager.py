from __future__ import annotations

import asyncio
from pathlib import Path

from mini_cc.agent.models import AgentConfig, generate_agent_id
from mini_cc.agent.sub_agent import SubAgent, build_worktree_notice
from mini_cc.agent.worktree import WorktreeService
from mini_cc.context.tool_use import ToolUseContext
from mini_cc.query_engine.engine import QueryEngine, StreamFn
from mini_cc.query_engine.state import Message, QueryState, Role
from mini_cc.task.models import AgentCompletionEvent, TaskType
from mini_cc.task.service import TaskService
from mini_cc.tool_executor.executor import StreamingToolExecutor
from mini_cc.tools import create_default_registry


class AgentManager:
    def __init__(
        self,
        *,
        project_root: Path,
        stream_fn: StreamFn,
        task_service: TaskService,
        completion_queue: asyncio.Queue[AgentCompletionEvent],
        worktree_service: WorktreeService | None = None,
    ) -> None:
        self._project_root = project_root
        self._stream_fn = stream_fn
        self._task_service = task_service
        self._completion_queue = completion_queue
        self._worktree_svc = worktree_service or WorktreeService(project_root)
        self._agents: dict[str, SubAgent] = {}

    @property
    def agents(self) -> dict[str, SubAgent]:
        return dict(self._agents)

    async def create_agent(
        self,
        *,
        prompt: str,
        sync: bool = True,
        fork: bool = False,
        parent_state: QueryState | None = None,
    ) -> SubAgent:
        agent_id = generate_agent_id()
        worktree_path = self._worktree_svc.create(agent_id)

        config = AgentConfig(
            agent_id=agent_id,
            worktree_path=str(worktree_path),
            is_fork=fork,
            parent_agent_id=None,
        )

        task = await self._task_service.create(
            type=TaskType.LOCAL_AGENT,
            subject=prompt[:80],
            description=prompt,
            agent_id=agent_id,
            prompt=prompt,
            is_fork=fork,
        )

        state = self._build_initial_state(config, fork, parent_state)
        engine = self._build_engine(config)
        output_dir = self._worktree_svc.output_dir

        agent = SubAgent(
            config=config,
            engine=engine,
            state=state,
            task_id=task.id,
            task_service=self._task_service,
            completion_queue=self._completion_queue,
            output_dir=output_dir,
        )

        self._agents[agent_id] = agent
        return agent

    def get_agent(self, agent_id: str) -> SubAgent | None:
        return self._agents.get(agent_id)

    async def cleanup(self, agent_id: str) -> None:
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            return
        self._worktree_svc.remove(agent_id)
        self._worktree_svc.cleanup_output(agent_id)

    def _build_initial_state(
        self,
        config: AgentConfig,
        fork: bool,
        parent_state: QueryState | None,
    ) -> QueryState:
        if fork and parent_state is not None:
            state = parent_state.model_copy(deep=True)
            notice = build_worktree_notice(config, self._project_root)
            state.messages.append(Message(role=Role.USER, content=notice))
            return state
        return QueryState()

    def _build_engine(self, config: AgentConfig) -> QueryEngine:
        registry = create_default_registry()
        executor = StreamingToolExecutor(registry)
        tool_use_ctx = ToolUseContext(
            get_schemas=registry.to_api_format,
            execute=executor.run,
        )
        return QueryEngine(stream_fn=self._stream_fn, tool_use_ctx=tool_use_ctx)

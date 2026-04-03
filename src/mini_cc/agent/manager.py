from __future__ import annotations

import asyncio
from pathlib import Path

from mini_cc.agent.models import AgentConfig, generate_agent_id
from mini_cc.agent.sub_agent import SubAgent, build_worktree_notice
from mini_cc.agent.worktree import WorktreeService
from mini_cc.context.system_prompt import EnvInfo, SystemPromptBuilder
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
        prompt_builder: SystemPromptBuilder | None = None,
        env_info: EnvInfo | None = None,
    ) -> None:
        self._project_root = project_root
        self._stream_fn = stream_fn
        self._task_service = task_service
        self._completion_queue = completion_queue
        self._worktree_svc = worktree_service or WorktreeService(project_root)
        self._prompt_builder = prompt_builder
        self._env_info = env_info
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
        mode: str = "build",
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

        state = self._build_initial_state(config, fork, parent_state, mode)
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
        mode: str = "build",
    ) -> QueryState:
        if fork and parent_state is not None:
            state = parent_state.model_copy(deep=True)
            notice = build_worktree_notice(config, self._project_root)
            state.messages.append(Message(role=Role.USER, content=notice))
            self._inject_sub_agent_notice(state, config)
            return state

        state = QueryState()
        if self._prompt_builder is not None and self._env_info is not None:
            sub_env = EnvInfo(
                working_directory=config.worktree_path,
                is_git_repo=self._env_info.is_git_repo,
                platform_name=self._env_info.platform_name,
                shell=self._env_info.shell,
                os_version=self._env_info.os_version,
                model_name=self._env_info.model_name,
                model_id=self._env_info.model_id,
            )
            system_content = self._prompt_builder.build(sub_env, mode=mode)
            state.messages.insert(0, Message(role=Role.SYSTEM, content=system_content))
        self._inject_sub_agent_notice(state, config)
        return state

    def _inject_sub_agent_notice(self, state: QueryState, config: AgentConfig) -> None:
        notice = (
            "\n\n## 子 Agent 身份声明\n"
            "你是一个子 Agent，由主 Agent 创建，用于独立执行特定任务。\n"
            "- 专注完成用户描述的任务，完成后立即结束\n"
            "- 你没有 agent 工具，不能创建子 Agent\n"
            f"- 你的工作目录（worktree）: {config.worktree_path}\n"
            f"- 原始项目路径: {self._project_root}\n"
            "- 引用文件时使用你的 worktree 路径"
        )
        if state.messages and state.messages[0].role == Role.SYSTEM:
            state.messages[0].content = (state.messages[0].content or "") + notice
        else:
            state.messages.insert(0, Message(role=Role.SYSTEM, content=notice))

    def _build_engine(self, config: AgentConfig) -> QueryEngine:
        registry = create_default_registry()
        executor = StreamingToolExecutor(registry)
        tool_use_ctx = ToolUseContext(
            get_schemas=registry.to_api_format,
            execute=executor.run,
        )
        return QueryEngine(stream_fn=self._stream_fn, tool_use_ctx=tool_use_ctx)

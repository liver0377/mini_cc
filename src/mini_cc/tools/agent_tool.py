from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from mini_cc.agent.manager import AgentManager
from mini_cc.agent.models import AgentStatus
from mini_cc.query_engine.state import QueryState, TextDelta
from mini_cc.tools.base import BaseTool, ToolResult


class AgentToolInput(BaseModel):
    prompt: str
    sync: bool = True
    fork: bool = False


class AgentTool(BaseTool):
    def __init__(
        self,
        manager: AgentManager,
        get_parent_state: Callable[[], QueryState],
        default_timeout: int = 120,
    ) -> None:
        self._manager = manager
        self._get_parent_state = get_parent_state
        self._default_timeout = default_timeout

    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        return (
            "创建子 Agent 执行任务。sync=True 时前台阻塞等待结果（超时转后台），"
            "sync=False 时后台运行。fork=True 时继承当前对话上下文。"
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return AgentToolInput

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(output="Use async_execute for agent tool")

    async def async_execute(self, **kwargs: Any) -> ToolResult:
        parsed = AgentToolInput.model_validate(kwargs)

        parent_state = self._get_parent_state() if parsed.fork else None

        agent = await self._manager.create_agent(
            prompt=parsed.prompt,
            sync=parsed.sync,
            fork=parsed.fork,
            parent_state=parent_state,
        )

        if parsed.sync or parsed.fork:
            return await self._execute_sync(agent, parsed.prompt)
        return await self._execute_async(agent, parsed.prompt)

    async def _execute_sync(self, agent: Any, prompt: str) -> ToolResult:
        timeout = self._default_timeout
        output_parts: list[str] = []

        async def _run_and_collect() -> None:
            async for event in agent.run(prompt):
                if isinstance(event, TextDelta):
                    output_parts.append(event.content)

        task = asyncio.create_task(_run_and_collect())
        done, pending = await asyncio.wait({task}, timeout=timeout)

        if pending:
            agent._status = AgentStatus.BACKGROUND_RUNNING
            return ToolResult(
                output=f"子 Agent {agent.config.agent_id} 已转为后台运行（超过 {timeout} 秒）"
            )

        return ToolResult(output="".join(output_parts))

    async def _execute_async(self, agent: Any, prompt: str) -> ToolResult:
        asyncio.create_task(agent.run_background(prompt))
        return ToolResult(
            output=f"异步子 Agent {agent.config.agent_id} 已启动，任务 ID: {agent.task_id}"
        )

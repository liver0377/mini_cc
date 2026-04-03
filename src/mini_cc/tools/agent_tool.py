from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from mini_cc.agent.manager import AgentManager
from mini_cc.agent.models import AgentStatus
from mini_cc.query_engine.state import (
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    QueryState,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


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
        event_queue: asyncio.Queue[Event] | None = None,
        get_mode: Callable[[], str] | None = None,
    ) -> None:
        self._manager = manager
        self._get_parent_state = get_parent_state
        self._default_timeout = default_timeout
        self._event_queue = event_queue
        self._get_mode = get_mode

    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        return (
            "创建一个独立的 sub-agent 来执行任务。sub-agent 拥有独立的对话上下文和 worktree，"
            "与你拥有相同的工具集。\n"
            "\n"
            "必须使用的场景（禁止自行串行处理）：\n"
            "- 用户要求分析项目、理解代码库、梳理架构 → 按模块/目录拆分为多个 agent 并行探索\n"
            "- 需要读取 3 个以上文件才能完成的任务\n"
            "- 多文件编辑（>2-3步）、可并行的独立任务\n"
            "\n"
            "不适用：单文件编辑、单次简单搜索、纯问答\n"
            "\n"
            "参数：\n"
            "- prompt（必填）：给 sub-agent 的自包含任务描述，需包含完整上下文和目标\n"
            "- sync（默认 true）：前台等待结果；false=后台运行\n"
            "- fork（默认 false）：true=继承当前对话历史\n"
            "\n"
            "注意：sub-agent 不可递归创建 sub-agent。详见工具使用策略中的 Sub-Agent 指南。"
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return AgentToolInput

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(output="Use async_execute for agent tool")

    async def async_execute(self, **kwargs: Any) -> ToolResult:
        parsed = AgentToolInput.model_validate(kwargs)
        logger.info("[agent-tool] called, prompt=%s", parsed.prompt[:120])
        mode = self._get_mode() if self._get_mode else "build"

        try:
            parent_state = self._get_parent_state() if parsed.fork else None

            agent = await self._manager.create_agent(
                prompt=parsed.prompt,
                sync=parsed.sync,
                fork=parsed.fork,
                parent_state=parent_state,
                mode=mode,
            )
        except Exception as e:
            return ToolResult(output=f"创建子 Agent 失败: {e}", success=False)

        if parsed.sync or parsed.fork:
            return await self._execute_sync(agent, parsed.prompt)
        return await self._execute_async(agent, parsed.prompt)

    async def _emit_event(self, event: Event) -> None:
        if self._event_queue is not None:
            await self._event_queue.put(event)

    async def _execute_sync(self, agent: Any, prompt: str) -> ToolResult:
        timeout = self._default_timeout
        output_parts: list[str] = []
        agent_id = agent.config.agent_id

        await self._emit_event(AgentStartEvent(agent_id=agent_id, task_id=agent.task_id, prompt=prompt[:80]))

        async def _run_and_collect() -> None:
            async for event in agent.run(prompt):
                if isinstance(event, TextDelta):
                    output_parts.append(event.content)
                elif isinstance(event, ToolCallStart):
                    await self._emit_event(AgentToolCallEvent(agent_id=agent_id, tool_name=event.name))
                elif isinstance(event, ToolResultEvent):
                    preview = event.output[:100] + ("..." if len(event.output) > 100 else "")
                    await self._emit_event(
                        AgentToolResultEvent(
                            agent_id=agent_id,
                            tool_name=event.name,
                            success=event.success,
                            output_preview=preview,
                        )
                    )

        task = asyncio.create_task(_run_and_collect())
        done, pending = await asyncio.wait({task}, timeout=timeout)

        if pending:
            agent._status = AgentStatus.BACKGROUND_RUNNING
            return ToolResult(output=f"子 Agent {agent_id} 已转为后台运行（超过 {timeout} 秒）")

        return ToolResult(output="".join(output_parts))

    async def _execute_async(self, agent: Any, prompt: str) -> ToolResult:
        await self._emit_event(
            AgentStartEvent(agent_id=agent.config.agent_id, task_id=agent.task_id, prompt=prompt[:80])
        )
        asyncio.create_task(agent.run_background(prompt))
        return ToolResult(output=f"异步子 Agent {agent.config.agent_id} 已启动，任务 ID: {agent.task_id}")

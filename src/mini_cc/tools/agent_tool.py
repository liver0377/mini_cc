from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from mini_cc.models import (
    AgentStartEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    Event,
    QueryState,
    TextDelta,
    ToolCallStart,
    ToolResultEvent,
)
from mini_cc.runtime.agents import AgentDispatcher, AgentDispatchRequest, AgentManager
from mini_cc.tools.base import BaseTool, ToolResult
from mini_cc.tools.plan_agents import AgentDispatchPlan

logger = logging.getLogger(__name__)


class AgentToolInput(BaseModel):
    prompt: str = ""
    readonly: bool = False
    fork: bool = False
    dispatch_plan_json: str | None = None
    scope_paths: list[str] = []


class AgentTool(BaseTool):
    def __init__(
        self,
        get_parent_state: Callable[[], QueryState],
        manager: AgentManager,
        dispatcher: AgentDispatcher | None = None,
        default_timeout: int = 120,
        get_mode: Callable[[], str] | None = None,
        get_run_id: Callable[[], str | None] | None = None,
        event_queue: asyncio.Queue[Event] | None = None,
        get_budget: Callable[[], Any] | None = None,
    ) -> None:
        self._manager = manager
        self._dispatcher = dispatcher or AgentDispatcher(manager=manager, get_budget=get_budget)
        self._get_parent_state = get_parent_state
        self._default_timeout = default_timeout
        self._get_mode = get_mode
        self._get_run_id = get_run_id
        self._event_queue = event_queue

    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        return (
            "创建一个独立的 sub-agent 来执行任务。\n"
            "\n"
            "两种模式：\n"
            "- 写 Agent（readonly=false，默认）：直写主工作区，同步阻塞，完成后自动 git commit\n"
            "- 只读 Agent（readonly=true）：在主工作区中使用只读工具探索代码，异步后台运行\n"
            "\n"
            "推荐流程：\n"
            "- 分析仓库前，先用 scan_dir 获取顶层目录/模块摘要，再按模块派发多个 readonly agent\n"
            "- 如果需要快速形成派工单，先调用 plan_agents，读取其 JSON 输出中的 dispatch_plan，再据此创建 agent\n"
            "- 发现明确修改边界后，再创建 write agent 执行修改和验证\n"
            "\n"
            "必须使用的场景（禁止自行串行处理）：\n"
            "- 用户要求分析项目、理解代码库、梳理架构 → readonly=true，按模块/目录拆分为多个 agent 并行探索\n"
            "- 需要读取 3 个以上文件才能完成的任务 → readonly=true\n"
            "- 代码修改、bug 修复、重构、写测试 → readonly=false，直写主工作区\n"
            "\n"
            "不适用：单文件编辑、单次简单搜索、纯问答\n"
            "\n"
            "参数：\n"
            "- prompt：给单个 sub-agent 的自包含任务描述，需包含完整上下文和目标\n"
            "- readonly（默认 false）：true=只读探索(异步后台)，false=写操作(同步+直写主工作区)\n"
            "- fork（默认 false）：true=继承当前对话历史（仅对写 Agent 有意义）\n"
            "- dispatch_plan_json：直接传入 plan_agents 的 JSON 输出，批量创建多个 readonly agent\n"
            "- scope_paths：写 Agent 声明自己负责的文件/目录范围；多个写 Agent 的 scope 不可重叠\n"
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
        logger.info("[agent-tool] called, prompt=%s, readonly=%s", parsed.prompt[:120], parsed.readonly)
        mode = self._get_mode() if self._get_mode else "build"
        run_id = self._get_run_id() if self._get_run_id else None

        if parsed.dispatch_plan_json:
            return await self._execute_dispatch_plan(parsed.dispatch_plan_json, mode, run_id)

        if not parsed.prompt.strip():
            return ToolResult(output="创建子 Agent 失败: prompt 不能为空", success=False)

        try:
            parent_state = self._get_parent_state() if parsed.fork else None

            agent = await self._dispatcher.dispatch(
                AgentDispatchRequest(
                    prompt=parsed.prompt,
                    readonly=parsed.readonly,
                    fork=parsed.fork,
                    parent_state=parent_state,
                    mode=mode,
                    scope_paths=parsed.scope_paths,
                    run_id=run_id,
                    role="analyzer" if parsed.readonly else "implementer",
                )
            )
        except Exception as e:
            return ToolResult(output=f"创建子 Agent 失败: {e}", success=False)

        if parsed.readonly:
            return await self._execute_readonly(agent, parsed.prompt)
        return await self._execute_write(agent, parsed.prompt)

    async def _emit_event(self, event: Event) -> None:
        if self._event_queue is not None:
            await self._event_queue.put(event)

    async def _execute_dispatch_plan(self, dispatch_plan_json: str, mode: str, run_id: str | None) -> ToolResult:
        try:
            plan = AgentDispatchPlan.model_validate_json(dispatch_plan_json)
        except Exception as err:
            return ToolResult(output=f"创建子 Agent 失败: dispatch_plan_json 无法解析: {err}", success=False)

        if not plan.dispatch_plan:
            return ToolResult(output="创建子 Agent 失败: dispatch_plan 不能为空", success=False)

        invalid_modes = [item.mode for item in plan.dispatch_plan if item.mode != "readonly"]
        if invalid_modes:
            return ToolResult(
                output=f"创建子 Agent 失败: 批量派工仅支持 readonly agent，收到 mode={invalid_modes[0]}",
                success=False,
            )
        for item in plan.dispatch_plan:
            pass
        requests = [
            AgentDispatchRequest(
                prompt=item.prompt,
                readonly=True,
                fork=False,
                parent_state=None,
                mode=mode,
                run_id=run_id,
                role="analyzer",
            )
            for item in plan.dispatch_plan
        ]
        try:
            agents = await self._dispatcher.dispatch_batch(requests)
        except Exception as err:
            return ToolResult(output=f"创建子 Agent 失败: {err}", success=False)
        created: list[dict[str, str | int]] = []
        for item, agent in zip(plan.dispatch_plan, agents, strict=False):
            asyncio.create_task(agent.run_background(item.prompt))
            created.append(
                {
                    "index": item.index,
                    "scope": item.scope,
                    "agent_id": agent.config.agent_id,
                    "task_id": agent.task_id,
                }
            )
        return ToolResult(
            output=json.dumps(
                {
                    "goal": plan.goal,
                    "created_count": len(created),
                    "agents": created,
                    "overflow_scopes": plan.overflow_scopes,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    async def _execute_write(self, agent: Any, prompt: str) -> ToolResult:
        output_parts: list[str] = []
        agent_id = agent.config.agent_id

        await self._emit_event(AgentStartEvent(agent_id=agent_id, task_id=agent.task_id, prompt=prompt[:80]))

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

        return ToolResult(output="".join(output_parts))

    async def _execute_readonly(self, agent: Any, prompt: str) -> ToolResult:
        asyncio.create_task(agent.run_background(prompt))
        return ToolResult(output=f"只读子 Agent {agent.config.agent_id} 已启动，任务 ID: {agent.task_id}")

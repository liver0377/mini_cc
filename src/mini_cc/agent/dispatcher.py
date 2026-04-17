from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from mini_cc.agent.manager import AgentManager
from mini_cc.agent.sub_agent import SubAgent
from mini_cc.models import QueryState

if TYPE_CHECKING:
    from mini_cc.harness.models import AgentBudget


class AgentDispatchRequest(BaseModel):
    prompt: str
    readonly: bool = False
    fork: bool = False
    parent_state: QueryState | None = None
    mode: str = "build"
    scope_paths: list[str] = Field(default_factory=list)
    run_id: str | None = None
    step_id: str | None = None
    work_item_id: str | None = None
    role: str | None = None


class AgentDispatcher:
    def __init__(
        self,
        *,
        manager: AgentManager,
        get_budget: Callable[[], AgentBudget | None] | None = None,
    ) -> None:
        self._manager = manager
        self._get_budget = get_budget

    async def dispatch(self, request: AgentDispatchRequest) -> SubAgent:
        budget = self._get_budget() if self._get_budget is not None else None
        budget_key = self._reserve_single_budget(request, budget)
        create_agent = cast(Any, self._manager.create_agent)
        try:
            return cast(SubAgent, await create_agent(**self._build_create_kwargs(request)))
        except Exception:
            if budget is not None and budget_key is not None:
                setattr(budget, budget_key, getattr(budget, budget_key) + 1)
            raise

    async def dispatch_batch(self, requests: list[AgentDispatchRequest]) -> list[SubAgent]:
        if not requests:
            return []
        if any(not request.readonly for request in requests):
            raise ValueError("batch dispatch only supports readonly agents")

        budget = self._get_budget() if self._get_budget is not None else None
        if budget is not None and len(requests) > budget.remaining_readonly:
            raise ValueError(
                f"批量派工需要 {len(requests)} 个只读 Agent，但剩余预算仅 {budget.remaining_readonly} 个。"
            )

        created: list[SubAgent] = []
        create_agent = cast(Any, self._manager.create_agent)
        for request in requests:
            try:
                agent = cast(SubAgent, await create_agent(**self._build_create_kwargs(request)))
            except Exception as err:
                if created:
                    raise ValueError(f"{err}（已创建 {len(created)} 个只读 Agent）") from err
                raise
            if budget is not None:
                budget.remaining_readonly -= 1
            created.append(agent)
        return created

    def _reserve_single_budget(self, request: AgentDispatchRequest, budget: AgentBudget | None) -> str | None:
        if budget is None:
            return None
        if request.readonly:
            if budget.remaining_readonly <= 0:
                raise ValueError("只读 Agent 预算已耗尽，无法创建新的子 Agent。请使用已有信息继续工作。")
            budget.remaining_readonly -= 1
            return "remaining_readonly"
        if budget.remaining_write <= 0:
            raise ValueError("写 Agent 预算已耗尽，无法创建新的子 Agent。请在当前上下文中完成工作。")
        budget.remaining_write -= 1
        return "remaining_write"

    def _build_create_kwargs(self, request: AgentDispatchRequest) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "prompt": request.prompt,
            "readonly": request.readonly,
            "fork": request.fork,
            "parent_state": request.parent_state,
            "mode": request.mode,
            "scope_paths": request.scope_paths,
            "run_id": request.run_id,
            "step_id": request.step_id,
            "work_item_id": request.work_item_id,
            "role": request.role,
        }
        parameters = inspect.signature(self._manager.create_agent).parameters
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
            return kwargs
        allowed = set(parameters)
        return {key: value for key, value in kwargs.items() if key in allowed}

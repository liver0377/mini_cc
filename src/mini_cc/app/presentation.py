from __future__ import annotations

from mini_cc.context.system_prompt import EnvInfo, SystemPromptBuilder
from mini_cc.models import Message, QueryState, Role

COMPACT_LABELS: dict[str, str] = {
    "auto": "上下文已自动压缩",
    "reactive": "上下文超出限制，已自动压缩后重试",
}


def rebuild_system_message(
    state: QueryState,
    prompt_builder: SystemPromptBuilder,
    env_info: EnvInfo,
    mode: str,
    run_id: str | None = None,
) -> None:
    content = prompt_builder.build(env_info, mode=mode, run_id=run_id)
    if state.messages and state.messages[0].role == Role.SYSTEM:
        state.messages[0] = Message(role=Role.SYSTEM, content=content)
    else:
        state.messages.insert(0, Message(role=Role.SYSTEM, content=content))

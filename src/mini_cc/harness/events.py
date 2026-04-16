from __future__ import annotations

from pydantic import BaseModel, Field

from mini_cc.harness.models import utc_now_iso


class HarnessEvent(BaseModel):
    event_type: str
    run_id: str
    timestamp: str = Field(default_factory=utc_now_iso)
    step_id: str | None = None
    message: str = ""
    data: dict[str, str] = Field(default_factory=dict)


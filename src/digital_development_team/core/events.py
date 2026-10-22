"""Event envelope. Khớp _assets/schemas/event.schema.json (contract test giữ đồng bộ).

EventDraft: agent/stage tạo, chưa có seq/ts. EventSink.emit gán seq + ts → Event.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from digital_development_team.core.enums import EventType, RiskTier, Stage


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LlmInfo(_Frozen):
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    prompt_hash: str | None = None
    transport: Literal["api", "cli"] = (
        "api"  # cli: Claude Code headless — loop trong adapter, tool qua MCP DDT, cost_usd=0
    )


class ToolInfo(_Frozen):
    name: str
    risk_tier: RiskTier | None = None
    approved_by: str | None = None  # policy | hook | human | session_memory
    denied_reason: str | None = None
    args_hash: str | None = None
    duration_ms: int | None = None
    status: str | None = None  # ok | error | denied


class HumanInfo(_Frozen):
    who: str
    gate: str | None = None
    choice: str | None = None
    note: str | None = None
    wait_sec: int | None = None


class BudgetSnapshot(_Frozen):
    tokens_used: int
    tokens_max: int
    turns_used: int
    turns_max: int
    wall_sec: int
    wall_max_sec: int


class EventDraft(_Frozen):
    issue_id: str  # IssueId | "global"
    actor: str
    event: EventType
    stage: Stage | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    llm: LlmInfo | None = None
    tool: ToolInfo | None = None
    artifact_ref: str | None = None
    hash: str | None = None
    human: HumanInfo | None = None
    budget: BudgetSnapshot | None = None


class Event(EventDraft):
    ts: AwareDatetime
    seq: int = Field(ge=0)

    def to_line(self) -> str:
        return self.model_dump_json(exclude_none=True) + "\n"

    @staticmethod
    def from_draft(draft: EventDraft, *, seq: int, ts: AwareDatetime) -> Event:
        return Event(**draft.model_dump(), seq=seq, ts=ts)

"""IssueState — bản mutable của Checkpoint trong lúc runner chạy. Serialize ↔ Checkpoint (Pydantic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from digital_development_team.core.enums import IssueStatus, Stage, Tier
from digital_development_team.core.models import BudgetUsage, Checkpoint

Phase = Literal["ready", "stage_done"]

PARALLEL_KEY = "_parallel"  # facts: danh sách stage chạy song song cùng stage hiện tại


@dataclass
class IssueState:
    issue_id: str
    stage: Stage = Stage.INGEST
    phase: Phase = "ready"
    status: IssueStatus = IssueStatus.RUNNING
    tier: Tier = Tier.T1
    rework: int = 0
    reloc: int = 0
    rejections: int = 0
    mg_retries: int = 0
    attempt: int = 1
    facts: dict[str, Any] = field(default_factory=dict)
    worktree: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    locks: tuple[str, ...] = ()
    pending_gate: str | None = None
    session_approvals: tuple[str, ...] = ()
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    leader_ctx_ref: str | None = None
    last_seq: int = -1

    # ───── chuyển đổi ─────
    @classmethod
    def new(cls, issue_id: str) -> IssueState:
        return cls(issue_id=issue_id)

    @classmethod
    def from_checkpoint(cls, cp: Checkpoint) -> IssueState:
        return cls(
            issue_id=cp.issue_id,
            stage=cp.stage,
            phase=cp.phase,
            status=cp.status,
            tier=cp.tier,
            rework=cp.rework,
            reloc=cp.reloc,
            rejections=cp.rejections,
            mg_retries=cp.mg_retries,
            attempt=cp.attempt,
            facts=dict(cp.facts),
            worktree=cp.worktree,
            branch=cp.branch,
            base_commit=cp.base_commit,
            locks=cp.locks,
            pending_gate=cp.pending_gate,
            session_approvals=cp.session_approvals,
            usage=cp.usage,
            leader_ctx_ref=cp.leader_ctx_ref,
            last_seq=cp.last_seq,
        )

    def to_checkpoint(self, *, saved_at: datetime) -> Checkpoint:
        return Checkpoint(
            issue_id=self.issue_id,
            stage=self.stage,
            phase=self.phase,
            status=self.status,
            tier=self.tier,
            rework=self.rework,
            reloc=self.reloc,
            rejections=self.rejections,
            mg_retries=self.mg_retries,
            attempt=self.attempt,
            facts=self.facts,
            worktree=self.worktree,
            branch=self.branch,
            base_commit=self.base_commit,
            locks=self.locks,
            pending_gate=self.pending_gate,
            session_approvals=self.session_approvals,
            usage=self.usage,
            leader_ctx_ref=self.leader_ctx_ref,
            last_seq=self.last_seq,
            saved_at=saved_at,
        )

    # ───── tiện ích ─────
    @property
    def terminal(self) -> bool:
        return self.stage.terminal

    @property
    def parallel(self) -> tuple[Stage, ...]:
        """Nhóm stage đang chạy song song (VERIFY ∥ REVIEW); rỗng = chỉ self.stage."""
        raw = self.facts.get(PARALLEL_KEY)
        return tuple(Stage(s) for s in raw) if raw else ()

    def set_parallel(self, stages: tuple[Stage, ...]) -> None:
        if len(stages) > 1:
            self.facts[PARALLEL_KEY] = [s.value for s in stages]
        else:
            self.facts.pop(PARALLEL_KEY, None)

    def g0_visits(self) -> int:
        return int(self.facts.get("g0_visits", 0))

    def bump_tier(self, to: Tier) -> bool:
        """MAX — không bao giờ hạ. True nếu đổi."""
        if to.rank > self.tier.rank:
            self.tier = to
            return True
        return False

    def merge_facts(self, facts: dict[str, Any]) -> None:
        for k, v in facts.items():
            if isinstance(v, dict) and isinstance(self.facts.get(k), dict):
                self.facts[k] = {**self.facts[k], **v}
            else:
                self.facts[k] = v

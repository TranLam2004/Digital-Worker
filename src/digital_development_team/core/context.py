"""Ngữ cảnh chạy xuyên tầng: hủy, đồng hồ, actor, budget tham chiếu.

CancelToken là cơ chế hủy DUY NHẤT trong lõi (sync + thread). Mọi vòng lặp dài phải kiểm nó.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from digital_development_team.core.enums import PermissionMode, Stage, Tier
from digital_development_team.core.errors import Cancelled


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Cho test: tự tăng khi gọi advance()."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)
        self._mono += seconds


class CancelToken:
    """Hủy hợp tác. Có token cha: cha hủy → con hủy. Có deadline: quá hạn → coi như hủy."""

    def __init__(self, *, parent: CancelToken | None = None, deadline_mono: float | None = None) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._parent = parent
        self._deadline = deadline_mono

    def cancel(self, reason: str = "cancelled") -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    def is_cancelled(self, *, mono_now: float | None = None) -> bool:
        if self._event.is_set():
            return True
        if self._parent is not None and self._parent.is_cancelled(mono_now=mono_now):
            self._reason = self._parent.reason or "parent cancelled"
            return True
        if self._deadline is not None:
            now = time.monotonic() if mono_now is None else mono_now
            if now >= self._deadline:
                self._reason = "deadline"
                return True
        return False

    @property
    def reason(self) -> str | None:
        return self._reason

    def raise_if_cancelled(self, *, mono_now: float | None = None) -> None:
        if self.is_cancelled(mono_now=mono_now):
            raise Cancelled(self._reason or "cancelled")

    def child(self, *, timeout_s: float | None = None, mono_now: float | None = None) -> CancelToken:
        deadline = None
        if timeout_s is not None:
            base = time.monotonic() if mono_now is None else mono_now
            deadline = base + timeout_s
        return CancelToken(parent=self, deadline_mono=deadline)

    def wait(self, timeout_s: float) -> bool:
        """Chờ tới khi bị hủy hoặc hết timeout. True nếu bị hủy."""
        return self._event.wait(timeout_s) or self.is_cancelled()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Actor(_Frozen):
    """Ai đang hành động. role 'runner', 'intake', 'dev'…; human 'human:P1'. sub_id cho subagent."""

    role: str
    sub_id: str | None = None

    def __str__(self) -> str:
        return f"{self.role}/{self.sub_id}" if self.sub_id else self.role

    @property
    def is_human(self) -> bool:
        return self.role.startswith("human:")

    @property
    def is_runner(self) -> bool:
        return self.role == "runner"


class BudgetRef(_Frozen):
    """Giới hạn cho một lần chạy agent. Giá trị tuyệt đối đã resolve từ budgets.yaml."""

    turns: int = Field(ge=1)
    wall_s: float = Field(gt=0)
    tokens: int = Field(ge=1)


@dataclass(frozen=True, slots=True)
class RunContext:
    """Ngữ cảnh một lần chạy agent (stage). Object runtime nội bộ — dataclass, không serialize.
    cancel/clock là object sống; Pydantic không isinstance được Protocol."""

    issue_id: str
    role: str
    stage: Stage
    tier: Tier
    budget: BudgetRef
    cancel: CancelToken
    clock: Clock
    actor: Actor
    attempt: int = 1
    rework: int = 0
    worktree: str | None = None
    allowed_files: tuple[str, ...] = ()
    permission_mode: PermissionMode = PermissionMode.READONLY
    model_role: str | None = (
        None  # role trong models.yaml để chọn model (vd dev_rework); None = role của agent
    )

    def llm_meta(self) -> dict[str, str]:
        return {"issue_id": self.issue_id, "role": self.role, "stage": self.stage.value}


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Ngữ cảnh truyền vào Tool.run. Tool không biết policy, chỉ biết nó đang chạy cho ai, ở đâu."""

    issue_id: str
    actor: Actor
    stage: Stage
    worktree: str | None
    home: str
    allowed_files: tuple[str, ...]
    cancel: CancelToken
    clock: Clock

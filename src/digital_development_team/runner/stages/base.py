"""StageHandler Protocol (runtime) + helper."""

from __future__ import annotations

from typing import Any, Protocol

from digital_development_team.core.enums import Stage
from digital_development_team.core.models import StageResult
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.state import IssueState


class StageHandler(Protocol):
    @property
    def stage(self) -> Stage: ...

    @property
    def uses_llm(self) -> bool: ...

    def run(self, state: IssueState, ctx: StageContext) -> StageResult: ...


def inner_model_role(inner: object, state: Any, cfg: Any, role: str | None) -> str:
    """Wrapper stage hỏi handler bên trong role model sẽ chạy (vd verifier_strong khi phục hồi) để runner tính
    budget + phụ phí transport đúng model đó."""
    pick = getattr(inner, "model_role_for", None)
    return str(pick(state, cfg)) if callable(pick) else str(role)


def result(stage: Stage, **facts: Any) -> StageResult:
    return StageResult(stage=stage, facts=facts)


WAITING = "waiting"
"""facts[WAITING] = True → runner coi stage chưa xong, status WAITING_GATE, không transition."""

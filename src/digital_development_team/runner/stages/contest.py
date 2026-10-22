"""TRIAGE cho item thi đấu: loại đã CHO SẴN trên trang match (CR/BUG/PI) — không để Intake đoán, không đi nhánh chờ người.

Intake vẫn chạy (độ phức tạp · tier · khu vực ảnh hưởng · gợi ý test), harness chỉ sửa các khoá định tuyến:
type theo đề · route security/human_first → standard · requires_human_before_code → false. TASK giữ phân loại của
Intake (có thể là tính năng mới hoặc chỉ cần điều tra), trừ khi Intake xếp vào nhánh an ninh/chờ người.
"""

from __future__ import annotations

from typing import Any

from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import StageResult
from digital_development_team.runner.context import RUNNER_ACTOR, StageContext
from digital_development_team.runner.match import contest_item
from digital_development_team.runner.stages.base import StageHandler, inner_model_role
from digital_development_team.runner.state import IssueState

FIXED_TYPE = {"CR": "change_request", "BUG": "bug", "PI": "production_incident"}
_HUMAN_ROUTES = ("security", "human_first")


def contest_intake(intake: dict[str, Any], item_type: str) -> dict[str, Any] | None:
    """Bản intake đã sửa cho item thi đấu; None nếu không cần sửa."""
    fixed = dict(intake)
    want = FIXED_TYPE.get(item_type)
    if want is not None:
        fixed["type"] = want
    elif fixed.get("type") == "security_incident":  # TASK bị xếp nhầm vào nhánh an ninh (luôn T3 + chờ người)
        fixed["type"] = "change_request"
    if fixed.get("route") in _HUMAN_ROUTES:
        fixed["route"] = "standard"
    if fixed.get("requires_human_before_code"):
        fixed["requires_human_before_code"] = False
    return fixed if fixed != intake else None


class ContestIntake:
    stage = Stage.TRIAGE
    uses_llm = True

    def __init__(self, inner: StageHandler) -> None:
        self._inner = inner
        self.role = getattr(inner, "role", None)

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        return inner_model_role(self._inner, state, cfg, self.role)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        res = self._inner.run(state, ctx)
        raw = contest_item(ctx.svc, state.issue_id)
        if raw is None or res.outcome is None or res.outcome.kind != "artifact":
            return res
        store = ctx.svc.store
        try:
            intake = dict(store.read(state.issue_id, "intake.yaml").content)
        except StoreError:
            return res
        fixed = contest_intake(intake, str(raw.get("item_type", "")))
        if fixed is None:
            return res
        ref = store.write(state.issue_id, "intake.yaml", fixed, actor=RUNNER_ACTOR)
        changed = {k: [intake.get(k), fixed.get(k)] for k in fixed if fixed.get(k) != intake.get(k)}
        outcome = res.outcome.model_copy(update={"uri": ref.uri})
        return res.model_copy(update={"outcome": outcome, "facts": {**res.facts, "contest_intake": changed}})

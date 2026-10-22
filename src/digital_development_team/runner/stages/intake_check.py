"""Intake self-consistency (Phase 11, mặc định tắt): Intake tự tin thấp → phân loại lần hai, lệch thì giữ bản thận trọng.

Bật: policy.yaml#intake_consistency.enabled. Chỉ khi intake.confidence < below_confidence. Hai bản khác nhau ở
type · risk_tier · route → giữ bản tier cao hơn (bằng nhau: bản không đi đường tắt `fast`), ghi lại thành phiên bản
mới nhất của intake.yaml, facts.intake_consistency ghi chỗ lệch. Tốn thêm một lượt Intake: khi bật, nới
budgets.yaml#stages.TRIAGE wall.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_development_team.core.context import Actor
from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import StageResult
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.base import StageHandler, inner_model_role
from digital_development_team.runner.state import IssueState

TIER_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
COMPARED = ("type", "risk_tier", "route")


def pick_cautious(first: Mapping[str, Any], second: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    a, b = TIER_RANK.get(str(first.get("risk_tier")), 1), TIER_RANK.get(str(second.get("risk_tier")), 1)
    if a != b:
        return (first, "first") if a > b else (second, "second")
    if (first.get("route") == "fast") != (second.get("route") == "fast"):
        return (second, "second") if first.get("route") == "fast" else (first, "first")
    return second, "second"


class IntakeConsistency:
    stage = Stage.TRIAGE
    uses_llm = True

    def __init__(self, inner: StageHandler) -> None:
        self._inner = inner
        self.role = getattr(inner, "role", None)  # runner cộng phụ phí transport CLI vào hạn stage theo role

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        return inner_model_role(self._inner, state, cfg, self.role)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        res = self._inner.run(state, ctx)
        cfg = ctx.config.policy.intake_consistency
        out = res.outcome
        if not cfg.enabled or out is None or out.kind != "artifact":
            return res
        store = ctx.svc.store
        try:
            first = dict(store.read(state.issue_id, "intake.yaml").content)
        except StoreError:
            return res
        conf = first.get("confidence")
        if not isinstance(conf, (int, float)) or conf >= cfg.below_confidence:
            return res
        res2 = self._inner.run(state, ctx)
        usage = res.usage + res2.usage
        if res2.outcome is None or res2.outcome.kind != "artifact":
            store.write(
                state.issue_id, "intake.yaml", first, actor=Actor(role="runner")
            )  # lần hai hỏng → giữ lần đầu
            return res.model_copy(
                update={"usage": usage, "facts": {**res.facts, "intake_consistency": {"second": "failed"}}}
            )
        second = dict(store.read(state.issue_id, "intake.yaml").content)
        diff = [k for k in COMPARED if first.get(k) != second.get(k)]
        info: dict[str, Any] = {
            "agree": not diff,
            "diff": diff,
            "confidence": [conf, second.get("confidence")],
        }
        facts = dict(res2.facts)
        if diff:
            kept, which = pick_cautious(first, second)
            info["kept"] = which
            if which == "first":
                store.write(state.issue_id, "intake.yaml", dict(kept), actor=Actor(role="runner"))
                facts["intake"] = {k: v for k, v in kept.items() if isinstance(v, (str, int, float, bool))}
        facts["intake_consistency"] = info
        return res2.model_copy(update={"usage": usage, "facts": facts})


__all__ = ["IntakeConsistency", "pick_cautious"]

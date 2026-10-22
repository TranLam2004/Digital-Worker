"""Phase 11 (tắt mặc định): Intake tự tin thấp → chạy lần hai; lệch thì giữ bản thận trọng (tier cao hơn)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import Actor, CancelToken
from digital_development_team.core.enums import Stage
from digital_development_team.core.models import AgentOutcome, BudgetUsage, StageResult
from digital_development_team.gates.service import GateService
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.intake_check import IntakeConsistency, pick_cautious
from digital_development_team.runner.stages.stub_data import stub_intake
from digital_development_team.runner.state import IssueState

ISSUE = "DDT-0001"


class _Intake:
    stage = Stage.TRIAGE
    uses_llm = True

    def __init__(self, rt: Runtime, answers: list[dict[str, Any]]) -> None:
        self.rt, self.answers, self.calls = rt, answers, 0

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        o = self.answers[self.calls]
        self.calls += 1
        ref = self.rt.store.write(ISSUE, "intake.yaml", stub_intake(ISSUE, o), actor=Actor(role="intake"))
        out = AgentOutcome.artifact(ref.uri, BudgetUsage(tokens_in=100))
        return StageResult(
            stage=Stage.TRIAGE,
            facts={"intake": {"risk_tier": o.get("risk_tier", "T1")}},
            outcome=out,
            usage=out.usage,
        )


def _run(rt: Runtime, answers: list[dict[str, Any]], *, enabled: bool) -> tuple[StageResult, _Intake]:
    cfg = load_config(overrides=[("policy", "intake_consistency.enabled", enabled)])
    inner = _Intake(rt, answers)
    svc = replace(rt.services(with_agents=False), config=cfg)
    ctx = StageContext(
        svc=svc,
        config=cfg,
        state=IssueState.new(ISSUE),
        cancel=CancelToken(),
        clock=rt.clock,
        gates=GateService(rt.db, rt.clock),
        budget=None,
    )
    return IntakeConsistency(inner).run(ctx.state, ctx), inner


def test_low_confidence_disagreement_keeps_cautious_version(runtime: Runtime) -> None:
    res, inner = _run(
        runtime,
        [{"confidence": 0.4, "risk_tier": "T2"}, {"confidence": 0.7, "risk_tier": "T0", "route": "fast"}],
        enabled=True,
    )
    assert inner.calls == 2 and res.usage.tokens_in == 200
    info = res.facts["intake_consistency"]
    assert info["agree"] is False and info["kept"] == "first" and set(info["diff"]) == {"risk_tier", "route"}
    assert (
        runtime.store.read(ISSUE, "intake.yaml").content["risk_tier"] == "T2"
    )  # bản thận trọng thành bản mới nhất
    assert res.facts["intake"]["risk_tier"] == "T2"


def test_off_or_confident_runs_once(runtime: Runtime) -> None:
    assert _run(runtime, [{"confidence": 0.4}], enabled=False)[1].calls == 1
    assert _run(runtime, [{"confidence": 0.9}], enabled=True)[1].calls == 1


def test_pick_cautious() -> None:
    assert (
        pick_cautious({"risk_tier": "T1", "route": "fast"}, {"risk_tier": "T1", "route": "standard"})[1]
        == "second"
    )
    assert pick_cautious({"risk_tier": "T3"}, {"risk_tier": "T1"})[1] == "first"


def test_every_agent_stage_handler_exposes_role() -> None:
    """Runner cộng phụ phí transport CLI vào hạn wall theo handler.role — wrapper làm mất role = hạn 30s cho TRIAGE
    (đo thật 2026-10-11: DDT-0006 BLOCKED ở TRIAGE vì IntakeConsistency thiếu role)."""
    from digital_development_team.runner.stages import default_handlers

    reg = default_handlers(stub=False)
    for stage in (
        Stage.TRIAGE,
        Stage.LOCALIZE,
        Stage.SPEC,
        Stage.IMPLEMENT,
        Stage.VERIFY,
        Stage.REVIEW,
        Stage.GATE,
        Stage.ANSWER,
    ):
        h = reg.get(stage)
        assert getattr(h, "role", None), stage
        assert callable(getattr(h, "model_role_for", None)), stage

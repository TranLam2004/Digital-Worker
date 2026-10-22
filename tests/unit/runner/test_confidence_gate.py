from __future__ import annotations

import pytest

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import EventType, Stage, Tier
from digital_development_team.eventlog.reader import read_events
from digital_development_team.risk.confidence import compute_confidence
from digital_development_team.runner import Runner
from digital_development_team.runner.stages import StageRegistry, default_handlers
from digital_development_team.runner.stages.gate import GateStage
from digital_development_team.runner.stages.stub import stub_handlers

W = load_config().policy.confidence_weights
EV_PASS = {
    "verdict": "pass",
    "criteria": [
        {"id": "AC1", "result": "pass"},
        {"id": "AC2", "result": "pass"},
        {"id": "AC3", "result": "pass"},
    ],
    "regression": {"result": "pass"},
}
SPEC = {"ambiguities": []}


def test_confidence_table_from_design() -> None:
    r = compute_confidence(
        W, evidence=EV_PASS, spec=SPEC, sec_report={"verdict": "pass"}, rework=0, reloc=0, familiarity=0.8
    )
    assert r.value == pytest.approx(0.87, abs=0.005) and r.weakest == "familiarity"
    r = compute_confidence(W, evidence=EV_PASS, spec=SPEC, sec_report={"verdict": "pass"}, rework=0, reloc=0)
    assert r.value == pytest.approx(0.81, abs=0.005)  # module lạ 0.4 → vừa đủ auto-merge T1
    r = compute_confidence(W, evidence=EV_PASS, spec=SPEC, sec_report={"verdict": "pass"}, rework=1, reloc=0)
    assert r.value == pytest.approx(0.71, abs=0.005) and r.weakest == "rework_penalty"
    inc = {**EV_PASS, "verdict": "inconclusive", "regression": {"result": "skipped"}}
    r = compute_confidence(
        W, evidence=inc, spec=SPEC, sec_report={"verdict": "pass"}, rework=0, reloc=0, familiarity=0.6
    )
    assert r.breakdown["verifier_agreement"] == 0.5 and r.breakdown["test_signal"] == 0.5
    amb = {"ambiguities": [{}, {}]}
    ev = {**EV_PASS, "criteria": [{"id": "AC1", "result": "pass"}, {"id": "AC2", "result": "unclear"}]}
    r = compute_confidence(W, evidence=ev, spec=amb, sec_report={"verdict": "fail"}, rework=0, reloc=0)
    assert r.breakdown["spec_clarity"] == pytest.approx(0.45) and r.breakdown["verifier_agreement"] == 0.0
    # security skipped (T0) → 0.9
    r = compute_confidence(W, evidence=EV_PASS, spec=SPEC, sec_report=None, rework=0, reloc=0)
    assert r.breakdown["verifier_agreement"] == 0.9 and "security skipped" in r.notes
    worst = compute_confidence(
        W, evidence={"verdict": "fail", "criteria": []}, spec=SPEC, sec_report=None, rework=2, reloc=1
    )
    assert worst.value < 0.1 and worst.weakest == "rework_penalty"


def _runner_with_gate(rt: Runtime, overrides: dict | None = None) -> Runner:  # type: ignore[type-arg]
    stubs = stub_handlers(overrides)
    reg = default_handlers(stub=True)
    merged = {**{s: reg.get(s) for s in reg.stages()}, **stubs, Stage.GATE: GateStage(stubs[Stage.GATE])}
    return Runner(rt.services(with_agents=False), handlers=StageRegistry(merged), stub=True)


def test_gate_rule_only_t0_skips_leader(runtime: Runtime) -> None:
    # familiarity mặc định 0.4 + security skipped (0.9) → conf 0.785 < 0.8; module quen (P3) mới đạt.
    # Hạ conf_proceed của preset autonomy đang dùng (balanced) để kiểm đường rule-only.
    from dataclasses import replace

    from digital_development_team.config.loader import load_config

    rt = replace(
        runtime,
        config=load_config(
            overrides=[
                ("policy", "autonomy_presets.balanced.conf_proceed", 0.75),
                ("policy", "autonomy_presets.balanced.conf_bump_range", [0.5, 0.75]),
            ]
        ),
    )
    rep = _runner_with_gate(rt, {"intake": {"risk_tier": "T0", "complexity": "trivial"}}).run(
        "DDT-0001", cancel=CancelToken()
    )
    assert rep.status == "done"
    dec = runtime.store.read("DDT-0001", "decision.json")
    assert (
        dec.ref.written_by == "runner"
        and dec.content["action"] == "merge"
        and dec.content["final_tier"] == "T0"
    )
    assert "Leader không gọi" in dec.content["rationale"] and dec.content["confidence"] >= 0.75
    cp = rt.checkpoints.load("DDT-0001")
    assert cp is not None and cp.facts["gate_rule_only"] is True and cp.facts["confidence"] >= 0.75
    assert not runtime.store.exists("DDT-0001", "sec_report.json")  # REVIEW skip T0
    gate_enter = next(
        e
        for e in read_events(runtime.layout, "DDT-0001")
        if e.event is EventType.STAGE_ENTER and e.stage is Stage.MERGE
    )
    assert gate_enter.data["via"]["rule_only"] is True


def test_gate_t1_calls_leader_with_confidence(runtime: Runtime) -> None:
    rep = _runner_with_gate(runtime).run("DDT-0001", cancel=CancelToken())
    # stub: evidence pass + security pass + spec 0 ambiguity + module lạ 0.4 → 0.81 ≥ 0.8 → T1 auto-merge (ví dụ "module mới" trong thiết kế)
    assert rep.status == "done"
    dec = runtime.store.read("DDT-0001", "decision.json")
    assert dec.ref.written_by == "leader"  # stub leader được gọi (không rule-only vì T1)
    cp = runtime.checkpoints.load("DDT-0001")
    assert cp is not None and cp.tier is Tier.T1 and cp.facts["confidence"] == 0.81
    assert cp.facts.get("gate_rule_only") is None and cp.facts["confidence_breakdown"]["familiarity"] == 0.4


def test_gate_bumps_tier_and_requires_human_when_sec_skipped(runtime: Runtime) -> None:
    # spec có 1 ambiguity → spec_clarity 0.8 → conf 0.77 → bump T2 → g1_required → chờ người dù Leader nói requires_human=false
    rep = _runner_with_gate(runtime, {"spec": {"ambiguities": [{"question": "?", "assumed": "x"}]}}).run(
        "DDT-0001", cancel=CancelToken()
    )
    assert rep.status == "waiting_gate" and rep.stage is Stage.G1_WAIT
    cp = runtime.checkpoints.load("DDT-0001")
    assert cp is not None and cp.tier is Tier.T2 and cp.facts["confidence"] == 0.77
    assert any("confidence" in r for r in cp.facts["tier_bumped_because"])

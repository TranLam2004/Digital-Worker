"""Escalation trong pipeline (04-escalation): agent hỏi → gate người → câu trả lời quay về đúng stage đã hỏi.

Trước bản này, kết quả escalation không có luật nào bắt nên issue đi tiếp như đã có artifact (SPEC → IMPLEMENT
không có spec), và facts tóm tắt che slot thật nên luật spec.ambiguities không bao giờ khớp.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_development_team.agents.brief import render_human_answers
from digital_development_team.app import Runtime
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, GateId, IssueStatus, Stage
from digital_development_team.core.models import GateDecision
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, RunReport, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.stub import stub_handlers
from tests.conftest import TESTDATA

ISSUE = "DDT-0001"
FORM = {
    "question": "Giữ tương thích API v1 đã deprecated hay chỉ sửa v2 cho issue này?",
    "options": [
        {"id": "A", "action": "Giữ v1, thêm adapter", "cost": "+2 file, ~15 phút", "risk": "low"},
        {"id": "B", "action": "Bỏ v1, chỉ sửa v2", "cost": "-120 dòng, ~5 phút", "risk": "medium"},
    ],
    "recommendation": "A",
}


def _runner(rt: Runtime, overrides: Mapping[str, Mapping[str, Any]]) -> Runner:
    reg = default_handlers(stub=True)
    reg = StageRegistry({**{s: reg.get(s) for s in reg.stages()}, **stub_handlers(overrides)})
    return Runner(rt.services(with_agents=False), handlers=reg, stub=True)


def _ingest(rt: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )


def _answer(rt: Runtime, clock: FakeClock, gate: GateId, choice: str, note: str | None = None) -> None:
    svc = GateService(rt.db, clock)
    og = svc.open_for(ISSUE, gate)
    assert og is not None, f"không có {gate.value} mở"
    svc.resolve(og.id, GateDecision(choice=choice, by="human:P2", at=clock.now(), note=note))


def _run(r: Runner) -> RunReport:
    return r.run(ISSUE, cancel=CancelToken())


def _entered(rt: Runtime, stage: Stage) -> int:
    return sum(
        1 for e in read_events(rt.layout, ISSUE) if e.event is EventType.STAGE_ENTER and e.stage is stage
    )


def test_spec_escalation_waits_at_g0_and_returns_to_spec(runtime: Runtime, fake_clock: FakeClock) -> None:
    _ingest(runtime)
    r = _runner(runtime, {"spec": {"escalate": FORM}})
    rep = _run(r)
    assert rep.status == "waiting_gate" and rep.stage is Stage.G0_WAIT
    assert not runtime.store.exists(ISSUE, "spec.yaml")  # chưa có spec thì không được sang IMPLEMENT
    og = GateService(runtime.db, fake_clock).open_for(ISSUE, GateId.G0)
    assert og is not None and og.escalation_uri and "escalation.yaml" in og.escalation_uri
    _answer(runtime, fake_clock, GateId.G0, "B", note="v1 không còn client nào")
    assert _run(r).status == "done"
    assert (
        _entered(runtime, Stage.LOCALIZE) == 1 and _entered(runtime, Stage.SPEC) == 2
    )  # quay về SPEC, không chạy lại Planner
    evs = read_events(runtime.layout, ISSUE)
    raised = [e for e in evs if e.event is EventType.ESCALATION_RAISED]
    assert len(raised) == 1 and raised[0].data["gate"] == "G0" and raised[0].data["raised_by"] == "analyst"
    override = [e for e in evs if e.event is EventType.HUMAN_OVERRIDE]
    assert (
        len(override) == 1 and override[0].data["recommendation"] == "A" and override[0].data["choice"] == "B"
    )
    form = runtime.store.read(ISSUE, "escalation.yaml").content
    assert form["resolved"]["choice"] == "B" and form["resolved"]["chosen_action"] == "Bỏ v1, chỉ sửa v2"
    assert form["resolved"]["by"] == "human:P2" and form["resolved"]["route"] == "continue"
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.status is IssueStatus.DONE
    answers = cp.facts["human_answers"]
    assert answers[0]["stage"] == "SPEC" and answers[0]["override"] is True
    text = render_human_answers(answers, role="dev")
    assert (
        text is not None and "human:P2 chose B: Bỏ v1, chỉ sửa v2" in text and "v1 không còn client" in text
    )
    assert render_human_answers(answers, role="verifier") is None  # Verifier độc lập


def test_implement_escalation_goes_to_g1_and_option_route_abort_blocks(
    runtime: Runtime, fake_clock: FakeClock
) -> None:
    _ingest(runtime)
    form = {
        **FORM,
        "type": "capability",
        "options": [
            {"id": "A", "action": "Thử cách khác với thư viện chuẩn", "cost": "~20 phút", "risk": "low"},
            {
                "id": "B",
                "action": "Dừng issue, cần người làm tay",
                "cost": "0",
                "risk": "low",
                "route": "abort",
            },
        ],
    }
    r = _runner(runtime, {"patch": {"escalate": form}})
    rep = _run(r)
    assert rep.status == "waiting_gate" and rep.stage is Stage.G1_WAIT  # IMPLEMENT mặc định hỏi ở G1
    _answer(runtime, fake_clock, GateId.G1, "B")
    rep = _run(r)
    assert rep.status == "blocked"
    assert runtime.store.read(ISSUE, "escalation.yaml").content["resolved"]["route"] == "abort"


def test_approve_on_escalation_card_means_recommendation(runtime: Runtime, fake_clock: FakeClock) -> None:
    _ingest(runtime)
    r = _runner(runtime, {"patch": {"escalate": FORM}})
    _run(r)
    _answer(runtime, fake_clock, GateId.G1, "approve")
    assert _run(r).status == "done"
    assert _entered(runtime, Stage.IMPLEMENT) == 2 and _entered(runtime, Stage.MERGE) == 1
    evs = read_events(runtime.layout, ISSUE)
    assert not [e for e in evs if e.event is EventType.HUMAN_OVERRIDE]  # chọn đúng khuyến nghị
    assert runtime.store.read(ISSUE, "escalation.yaml").content["resolved"]["choice"] == "A"


def test_g0_escalation_timeout_takes_recommendation(runtime: Runtime, fake_clock: FakeClock) -> None:
    _ingest(runtime)
    r = _runner(
        runtime, {"spec": {"escalate": {**FORM, "default_on_timeout": "proceed_with_recommendation"}}}
    )
    assert _run(r).stage is Stage.G0_WAIT
    fake_clock.advance(91)  # G0 balanced: 90 giây
    assert _run(r).status == "done"
    form = runtime.store.read(ISSUE, "escalation.yaml").content
    assert (
        form["resolved"]["choice"] == "timeout"
        and form["resolved"]["chosen_action"] == "Giữ v1, thêm adapter"
    )
    evs = read_events(runtime.layout, ISSUE)
    assert any(e.event is EventType.GATE_TIMEOUT for e in evs)
    assert not any(e.event is EventType.HUMAN_OVERRIDE for e in evs)


def test_third_ask_is_answered_by_harness(runtime: Runtime, fake_clock: FakeClock) -> None:
    _ingest(runtime)
    r = _runner(runtime, {"spec": {"escalate": {**FORM, "times": 3}}})
    for _ in range(2):
        assert _run(r).stage is Stage.G0_WAIT
        _answer(runtime, fake_clock, GateId.G0, "A")
    assert _run(r).status == "done"  # lần hỏi thứ 3: harness tự chọn khuyến nghị, không mở gate
    tickets = runtime.db.all("SELECT gate FROM gate_requests WHERE issue_id=?", (ISSUE,))
    assert [t["gate"] for t in tickets].count("G0") == 2
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.facts["asks"]["analyst"] == 3
    assert cp.facts["human_answers"][-1]["by"] == "system"


def test_spec_ambiguity_rule_reads_real_slot(runtime: Runtime) -> None:
    """Luật SPEC → G0 khi spec mơ hồ và intake chưa chắc. Trước đây facts tóm tắt (bỏ list) che slot → không khớp."""
    _ingest(runtime)
    ov = {
        "intake": {"confidence": 0.65},
        "spec": {"ambiguities": [{"question": "v1 hay v2?", "assumed": "v2"}]},
    }
    rep = _run(_runner(runtime, ov))
    assert rep.status == "waiting_gate" and rep.stage is Stage.G0_WAIT


def test_answered_form_is_not_attached_to_later_ticket(runtime: Runtime, fake_clock: FakeClock) -> None:
    _ingest(runtime)
    r = _runner(
        runtime,
        {"spec": {"escalate": FORM}, "intake": {"risk_tier": "T2"}, "decision": {"requires_human": True}},
    )
    _run(r)
    _answer(runtime, fake_clock, GateId.G0, "A")
    rep = _run(r)
    assert rep.status == "waiting_gate" and rep.stage is Stage.G1_WAIT
    g1 = GateService(runtime.db, fake_clock).open_for(ISSUE, GateId.G1)
    assert g1 is not None and g1.escalation_uri is None  # G1 thường: card duyệt, không phải card A/B


def test_bad_form_is_rejected_before_any_human_sees_it(runtime: Runtime) -> None:
    import pytest

    from digital_development_team.core.context import Actor
    from digital_development_team.core.errors import SchemaError

    _ingest(runtime)
    base = {
        "issue_id": ISSUE,
        "raised_by": "dev",
        "stage": "IMPLEMENT",
        "type": "capability",
        "question": FORM["question"],
        "options": FORM["options"],
        "recommendation": "C",
        "confidence": 0.5,
        "blocked_since": "2026-10-03T09:00:00+00:00",
    }
    with pytest.raises(SchemaError) as e:
        runtime.store.write(ISSUE, "escalation.yaml", base, actor=Actor(role="dev"))
    paths = {v.path for v in e.value.violations}
    assert paths == {"recommendation", "options"}  # khuyến nghị lạ · capability thiếu đường dừng
    ok = {
        **base,
        "recommendation": "A",
        "options": [*FORM["options"][:1], {**FORM["options"][1], "route": "abort"}],
    }
    runtime.store.write(ISSUE, "escalation.yaml", ok, actor=Actor(role="dev"))
    rejected = [e for e in read_events(runtime.layout, ISSUE) if e.event is EventType.SLOT_REJECTED]
    assert len(rejected) == 1


def test_handover_lists_human_answers(runtime: Runtime, fake_clock: FakeClock) -> None:
    from digital_development_team.reporting import build_handover

    _ingest(runtime)
    r = _runner(runtime, {"spec": {"escalate": FORM}})
    _run(r)
    _answer(runtime, fake_clock, GateId.G0, "B", note="v1 không còn client nào")
    _run(r)
    md = build_handover(runtime.layout, runtime.db, runtime.store, ISSUE, now=fake_clock.now()).markdown
    assert "## Người đã trả lời câu hỏi của agent" in md
    assert "P2 chọn B: Bỏ v1, chỉ sửa v2" in md and "v1 không còn client nào" in md

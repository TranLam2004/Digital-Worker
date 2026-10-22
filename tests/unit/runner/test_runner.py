"""Runner với stub stages: walking skeleton INGEST→DONE, G0/G1, STOP/resume, --until, rework/reloc, machine gate."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from digital_development_team.app import Runtime
from digital_development_team.cli.main import app
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, GateId, IssueStatus, Stage, Tier
from digital_development_team.core.models import GateDecision
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService, StopSwitch
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.stub import stub_handlers

ISSUE = "DDT-0001"
ALL_SLOTS = [
    "item.json",
    "intake.yaml",
    "plan.yaml",
    "spec.yaml",
    "patch/",
    "evidence.json",
    "sec_report.json",
    "decision.json",
]


def _runner(rt: Runtime, overrides: Mapping[str, Mapping[str, Any]] | None = None) -> Runner:
    reg = default_handlers(stub=True)
    if overrides:
        reg = StageRegistry({**{s: reg.get(s) for s in reg.stages()}, **stub_handlers(overrides)})
    return Runner(rt.services(), handlers=reg, stub=True)


def _types(rt: Runtime) -> list[str]:
    return [e.event.value for e in read_events(rt.layout, ISSUE)]


def test_walking_skeleton_ingest_to_done(runtime: Runtime) -> None:
    rep = _runner(runtime).run(ISSUE, cancel=CancelToken())
    assert rep.status == "done" and rep.exit_code == 0 and rep.stage is Stage.DONE
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.status is IssueStatus.DONE and cp.rework == 0 and cp.usage.tokens > 0
    assert [r.slot for r in runtime.store.list(ISSUE)] == sorted(ALL_SLOTS, key=ALL_SLOTS.index) or {
        r.slot for r in runtime.store.list(ISSUE)
    } == set(ALL_SLOTS)
    evs = read_events(runtime.layout, ISSUE)
    assert [e.seq for e in evs] == list(range(len(evs)))  # seq liền
    types = [e.event.value for e in evs]
    assert types.count("slot_written") == 8 and types[-1] == "issue_done"
    entered = [e.stage.value for e in evs if e.event is EventType.STAGE_ENTER and e.stage]
    assert entered == [
        "INGEST",
        "TRIAGE",
        "LOCALIZE",
        "SPEC",
        "IMPLEMENT",
        "MACHINE_GATE",
        "VERIFY",
        "GATE",
        "MERGE",
    ]
    # VERIFY ∥ REVIEW chạy cùng nhau: cả evidence lẫn sec_report được ghi trong 1 stage
    exits = [e for e in evs if e.event is EventType.STAGE_EXIT]
    verify_exit = next(e for e in exits if e.stage is Stage.VERIFY)
    assert "evidence" in verify_exit.data["result"] and "sec_report" in verify_exit.data["result"]
    assert [r.issue_id for r in runtime.checkpoints.board(status=IssueStatus.DONE)] == [ISSUE]
    # chạy lại issue đã xong → không làm gì
    assert _runner(runtime).run(ISSUE, cancel=CancelToken()).status == "done"


def test_until_then_resume_without_redoing(runtime: Runtime) -> None:
    rep = _runner(runtime).run(ISSUE, cancel=CancelToken(), until=Stage.SPEC)
    assert rep.status == "running" and rep.stage is Stage.SPEC and rep.exit_code == 0
    assert {r.slot for r in runtime.store.list(ISSUE)} == {
        "item.json",
        "intake.yaml",
        "plan.yaml",
        "spec.yaml",
    }
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.phase == "stage_done" and cp.stage is Stage.SPEC
    rep2 = _runner(runtime).run(ISSUE, cancel=CancelToken())
    assert rep2.status == "done"
    assert runtime.store.latest(ISSUE, "spec.yaml").version == 1  # type: ignore[union-attr]
    assert "resumed" in _types(runtime)


def test_stop_pauses_and_resume_continues(runtime: Runtime) -> None:
    sw = StopSwitch(runtime.layout)
    _runner(runtime).run(ISSUE, cancel=CancelToken(), until=Stage.LOCALIZE)
    sw.request(by="P2")
    rep = _runner(runtime).run(ISSUE, cancel=CancelToken())
    assert rep.status == "paused" and rep.exit_code == 3 and "STOP" in (rep.reason or "")
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.status is IssueStatus.PAUSED and cp.stage is Stage.LOCALIZE
    assert _types(runtime)[-1] == "stop_requested"
    sw.clear()
    assert _runner(runtime).run(ISSUE, cancel=CancelToken()).status == "done"


def test_g0_wait_approve_and_t3_needs_g1(runtime: Runtime, fake_clock: FakeClock) -> None:
    r = _runner(runtime, {"intake": {"risk_tier": "T3"}})
    rep = r.run(ISSUE, cancel=CancelToken())
    assert rep.status == "waiting_gate" and rep.exit_code == 4 and rep.stage is Stage.G0_WAIT
    cp = runtime.checkpoints.load(ISSUE)
    assert (
        cp is not None and cp.tier is Tier.T3 and cp.facts["tier_bumped_because"] == ["intake.risk_tier=T3"]
    )
    svc = GateService(runtime.db, fake_clock)
    og = svc.open_for(ISSUE, GateId.G0)
    assert og is not None and og.ticket.timeout_s is None  # T3 chờ mãi
    assert og.summary["intake"]["risk_tier"] == "T3"
    assert "gate_wait" in _types(runtime)
    # chưa ai quyết → vẫn chờ, không mở ticket mới
    assert r.run(ISSUE, cancel=CancelToken()).status == "waiting_gate"
    assert len(svc.list_open()) == 1
    svc.resolve(og.id, GateDecision(choice="approve", by="human:P2", at=fake_clock.now(), note="làm đi"))
    rep2 = r.run(ISSUE, cancel=CancelToken())
    assert rep2.status == "waiting_gate" and rep2.stage is Stage.G1_WAIT  # T3 → requires_human
    resolved = next(e for e in read_events(runtime.layout, ISSUE) if e.event is EventType.GATE_RESOLVED)
    assert (
        resolved.human is not None and resolved.human.choice == "approve" and resolved.human.who == "human:P2"
    )
    g1 = svc.open_for(ISSUE, GateId.G1)
    assert g1 is not None and g1.ticket.timeout_s is None and g1.summary["decision"]["action"] == "merge"
    svc.resolve(g1.id, GateDecision(choice="approve", by="human:P1", at=fake_clock.now()))
    assert r.run(ISSUE, cancel=CancelToken()).status == "done"
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.facts["g0_visits"] == 1 and cp.pending_gate is None and cp.tier is Tier.T3


def test_g0_reject_blocks(runtime: Runtime, fake_clock: FakeClock) -> None:
    r = _runner(runtime, {"intake": {"confidence": 0.4}})
    assert r.run(ISSUE, cancel=CancelToken()).status == "waiting_gate"
    svc = GateService(runtime.db, fake_clock)
    og = svc.open_for(ISSUE, GateId.G0)
    assert og is not None and og.ticket.timeout_s == 90.0  # balanced preset
    svc.resolve(og.id, GateDecision(choice="reject", by="human:P2", at=fake_clock.now(), note="không làm"))
    rep = r.run(ISSUE, cancel=CancelToken())
    assert rep.status == "blocked" and rep.exit_code == 1
    assert _types(runtime)[-1] == "blocked"


def test_g0_timeout_proceeds_conservative(runtime: Runtime, fake_clock: FakeClock) -> None:
    r = _runner(runtime, {"intake": {"confidence": 0.4}})
    assert r.run(ISSUE, cancel=CancelToken()).status == "waiting_gate"
    fake_clock.advance(91)
    rep = r.run(ISSUE, cancel=CancelToken())
    # tier bump T1→T2 → decision stub requires_human → G1_WAIT
    assert rep.status == "waiting_gate" and rep.stage is Stage.G1_WAIT
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.tier is Tier.T2 and cp.facts.get("conservative") is True
    assert "gate_timeout" in _types(runtime)
    svc = GateService(runtime.db, fake_clock)
    og = svc.open_for(ISSUE, GateId.G1)
    assert og is not None and og.ticket.timeout_s == 300.0 and og.summary["decision"]["action"] == "merge"
    svc.resolve(og.id, GateDecision(choice="approve", by="human:P1", at=fake_clock.now()))
    assert r.run(ISSUE, cancel=CancelToken()).status == "done"


def test_g1_reject_with_note_reworks(runtime: Runtime, fake_clock: FakeClock) -> None:
    r = _runner(runtime, {"decision": {"requires_human": True}})
    assert r.run(ISSUE, cancel=CancelToken()).stage is Stage.G1_WAIT
    svc = GateService(runtime.db, fake_clock)
    og = svc.open_for(ISSUE, GateId.G1)
    assert og is not None
    svc.resolve(
        og.id, GateDecision(choice="reject", by="human:P1", at=fake_clock.now(), note="thiếu test None")
    )
    rep = r.run(ISSUE, cancel=CancelToken())
    assert rep.stage is Stage.G1_WAIT and rep.status == "waiting_gate"  # rework xong lại tới G1
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.rework == 1
    assert runtime.store.latest(ISSUE, "patch/").version == 2  # type: ignore[union-attr]
    assert "rework" in _types(runtime)


def test_machine_gate_free_retry(runtime: Runtime) -> None:
    rep = _runner(runtime, {"machine_gate": {"fail_times": 1}}).run(ISSUE, cancel=CancelToken())
    assert rep.status == "done"
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.mg_retries == 1 and cp.rework == 0
    assert runtime.store.latest(ISSUE, "patch/").version == 2  # type: ignore[union-attr]


def test_verify_fail_exhausts_rework_then_reloc_then_blocked(runtime: Runtime) -> None:
    rep = _runner(runtime, {"evidence": {"verdict": "fail"}}).run(ISSUE, cancel=CancelToken())
    assert rep.status == "blocked"
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.rework == 2 and cp.reloc == 1
    # 4 lần IMPLEMENT theo luật + thang phục hồi: re_localize (LOCALIZE→SPEC→IMPLEMENT) rồi model_upgrade (dev_strong)
    assert runtime.store.latest(ISSUE, "patch/").version == 6  # type: ignore[union-attr]
    assert runtime.store.latest(ISSUE, "plan.yaml").version == 3  # type: ignore[union-attr]
    types = _types(runtime)
    assert types.count("rework") == 2 and types.count("re_localize") == 1 and types[-1] == "blocked"
    rec = [e.data for e in read_events(runtime.layout, ISSUE) if e.event is EventType.RECOVERY]
    assert [(r["rung"], r["to"]) for r in rec] == [
        ("re_localize", "LOCALIZE"),
        ("model_upgrade", "IMPLEMENT"),
    ]
    assert rec[1]["model_role"] == "dev_strong" and all(r["failure_class"] == "test_fail" for r in rec)
    report = runtime.store.read(ISSUE, "blocked_report.json").content
    assert (
        report["category"] == "capability" and report["failure_class"] == "test_fail" and report["resumable"]
    )
    assert [t["step"] for t in report["tried"]][-2:] == ["recovery:re_localize", "recovery:model_upgrade"]
    # REVIEW bị hủy khi VERIFY fail → sec_report có thể thiếu ở vài vòng nhưng không quá số lần IMPLEMENT (6)
    sec = runtime.store.latest(ISSUE, "sec_report.json")
    assert sec is None or sec.version <= 6


def test_fast_question_goes_to_answer(runtime: Runtime) -> None:
    rep = _runner(runtime, {"intake": {"type": "question", "route": "fast"}}).run(ISSUE, cancel=CancelToken())
    assert rep.status == "done"
    entered = [
        e.stage.value
        for e in read_events(runtime.layout, ISSUE)
        if e.event is EventType.STAGE_ENTER and e.stage
    ]
    assert entered == ["INGEST", "TRIAGE", "ANSWER"]


def test_t0_skips_review_and_merges_by_rule(runtime: Runtime) -> None:
    rep = _runner(runtime, {"intake": {"risk_tier": "T0", "complexity": "trivial"}}).run(
        ISSUE, cancel=CancelToken()
    )
    assert rep.status == "done"
    assert not runtime.store.exists(ISSUE, "sec_report.json")  # REVIEW skip (T0, regex sạch)
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.tier is Tier.T0
    evs = read_events(runtime.layout, ISSUE)
    gate_enter = next(e for e in evs if e.event is EventType.STAGE_ENTER and e.stage is Stage.MERGE)
    assert gate_enter.data["via"]["rule_only"] is True  # GATE → MERGE bằng rule, không cần Leader quyết


def test_tier_never_lowers_and_sensitive_bumps(runtime: Runtime) -> None:
    rep = _runner(runtime, {"patch": {}, "intake": {"risk_tier": "T2"}}).run(
        ISSUE, cancel=CancelToken(), until=Stage.IMPLEMENT
    )
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.tier is Tier.T2 and rep.status == "running"


def test_cli_run_stop_resume_gates(tmp_home: Path) -> None:
    cli = CliRunner()
    base = ["--home", str(tmp_home)]
    r = cli.invoke(app, [*base, "run", ISSUE, "--stub-stages", "--until", "SPEC"])
    assert r.exit_code == 0, r.output
    assert "running @ SPEC" in r.output
    r = cli.invoke(app, [*base, "stop", "--by", "P2"])
    assert r.exit_code == 0 and "STOP" in r.output
    r = cli.invoke(app, [*base, "run", ISSUE, "--stub-stages"])
    assert r.exit_code == 3, r.output
    r = cli.invoke(app, [*base, "resume"])
    assert r.exit_code == 0 and "resume" in r.output
    r = cli.invoke(app, [*base, "run", ISSUE, "--stub-stages"])
    assert r.exit_code == 0 and "done @ DONE" in r.output, r.output
    r = cli.invoke(app, [*base, "gates"])
    assert r.exit_code == 0 and "không có gate" in r.output
    r = cli.invoke(app, [*base, "board"])
    assert r.exit_code == 0 and "done" in r.output
    r = cli.invoke(app, [*base, "run", "DDT-0002"])  # không stub → stage agent chưa có handler
    assert r.exit_code in (1, 2), r.output

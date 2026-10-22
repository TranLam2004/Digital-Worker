"""B11: pipeline thật (mock golden) tới hết GATE: MACHINE_GATE chạy pytest thật, Verifier/Security/Leader thật; MERGE B12."""

from __future__ import annotations

from pathlib import Path

from digital_development_team.app import build_runtime
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, GateId, Stage
from digital_development_team.core.models import GateDecision
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0001"
GOLDEN = TESTDATA / "golden" / "calc_mean"


def test_pipeline_until_gate(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    _, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN)
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    rep = Runner(rt.services(), handlers=default_handlers(stub=False)).run(
        ISSUE, cancel=CancelToken(), until=Stage.GATE
    )
    assert rep.status == "running" and rep.stage is Stage.GATE, rep
    cp = rt.checkpoints.load(ISSUE)
    assert cp is not None
    # MACHINE_GATE thật: pytest trong worktree pass, checklist regex sạch, harness điền evidence
    mg = cp.facts["machine_gate"]
    assert (
        mg["any_fail"] is False
        and mg["unit_test"] == "pass"
        and mg["regression"] == "pass"
        and mg["lint"] == "skipped"
    )
    assert set(mg["files_touched"]) == {"calc.py", "tests/test_calc.py"} and mg["scope_violation"] == []
    assert cp.facts["checklist"]["clean"] is True and cp.facts["checklist"]["security_regex_pass"] is True
    ev = rt.store.read(ISSUE, "evidence.json").content
    assert (
        ev["version"] == 1 and ev["machine_gate"]["unit_test"]["status"] == "pass" and ev["verdict"] == "pass"
    )
    assert ev["scope_violation"] == [] and any(r["id"] == "CODE-001" for r in ev["checklist_results"])
    assert ev["machine_gate"]["unit_test"]["output_ref"].startswith("artifact://")
    sec = rt.store.read(ISSUE, "sec_report.json").content
    assert sec["mode"] == "lite" and sec["verdict"] == "pass"
    dec = rt.store.read(ISSUE, "decision.json")
    assert (
        dec.ref.written_by == "leader"
        and dec.content["action"] == "merge"
        and dec.content["requires_human"] is False
    )
    # confidence harness: 1 ambiguity (0.8) + module lạ (0.4) → 0.77 → bump T2 → cần người ở G1
    assert cp.facts["confidence"] == 0.77 and cp.facts["confidence_breakdown"]["test_signal"] == 1.0
    assert cp.tier.value == "T2"
    for sid in ("intake", "plan", "spec", "evidence", "sec_report", "decision"):
        rt.registry.raise_if_invalid(
            sid,
            rt.store.read(
                ISSUE,
                {
                    "intake": "intake.yaml",
                    "plan": "plan.yaml",
                    "spec": "spec.yaml",
                    "evidence": "evidence.json",
                    "sec_report": "sec_report.json",
                    "decision": "decision.json",
                }[sid],
            ).content,
        )
    evs = read_events(rt.layout, ISSUE)
    types = [e.event for e in evs]
    assert (
        EventType.CHECKLIST_RUN in types and types.count(EventType.LLM_CALL) == 5 + 4 + 2 + 2 + 1
    )  # intake1 planner3 analyst1 dev4 verifier2 security2 leader1
    assert [e.seq for e in evs] == list(range(len(evs)))
    # VERIFY ∥ REVIEW: cả hai ghi slot trong cùng stage
    verify_exit = next(e for e in evs if e.event is EventType.STAGE_EXIT and e.stage is Stage.VERIFY)
    assert "evidence" in verify_exit.data["result"] and "sec_report" in verify_exit.data["result"]
    # harness ép G1 (g1_required) dù Leader nói requires_human=false → chờ người
    rep2 = Runner(rt.services(), handlers=default_handlers(stub=False)).run(ISSUE, cancel=CancelToken())
    assert rep2.status == "waiting_gate" and rep2.stage is Stage.G1_WAIT
    svc = GateService(rt.db, rt.clock)
    og = svc.open_for(ISSUE, GateId.G1)
    assert og is not None and og.ticket.timeout_s == 300.0 and og.summary["decision"]["action"] == "merge"
    svc.resolve(og.id, GateDecision(choice="approve", by="human:P1", at=rt.clock.now()))
    rep3 = Runner(rt.services(), handlers=default_handlers(stub=False)).run(ISSUE, cancel=CancelToken())
    assert rep3.status == "done"  # MERGE (B12): rebase · gate sau rebase · ff-only · dọn worktree
    rt.close()

"""reporting.stats (suy từ events) · reporting.handover (biên bản bàn giao) · /api/reports."""

from __future__ import annotations

from digital_development_team.app import Runtime
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, GateId, Stage
from digital_development_team.core.events import Event, HumanInfo, LlmInfo, ToolInfo
from digital_development_team.core.models import GateDecision
from digital_development_team.gates import GateService
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.reporting import build_handover, build_report, derive_role_stats, render_report
from digital_development_team.reporting.stats import role_stats_json
from digital_development_team.runner import Runner, default_handlers
from tests.conftest import TESTDATA

ISSUE = "DDT-0001"


def _ev(
    seq: int, event: EventType, *, actor: str = "runner", stage: Stage | None = None, **kw: object
) -> Event:
    return Event(
        issue_id=ISSUE, seq=seq, ts="2026-10-03T09:00:00+00:00", actor=actor, event=event, stage=stage, **kw
    )  # type: ignore[arg-type]


def test_derive_role_stats_from_synthetic_events() -> None:
    evs = [
        _ev(0, EventType.STAGE_ENTER, stage=Stage.IMPLEMENT),
        _ev(
            1,
            EventType.LLM_CALL,
            actor="dev",
            stage=Stage.IMPLEMENT,
            llm=LlmInfo(
                model="m", tokens_in=1000, tokens_out=100, tokens_cached=600, latency_ms=900, cost_usd=0.02
            ),
        ),
        _ev(
            2,
            EventType.TOOL_CALL,
            actor="dev",
            stage=Stage.IMPLEMENT,
            tool=ToolInfo(name="fs.edit", status="ok"),
        ),
        _ev(
            3,
            EventType.TOOL_DENIED,
            actor="dev",
            stage=Stage.IMPLEMENT,
            tool=ToolInfo(name="exec.run", denied_reason="deny list"),
        ),
        _ev(
            4,
            EventType.LLM_CALL,
            actor="dev/sub-1",
            stage=Stage.IMPLEMENT,
            llm=LlmInfo(model="m", tokens_in=500, tokens_out=50),
        ),
        _ev(5, EventType.STAGE_ENTER, stage=Stage.VERIFY),
        _ev(
            6,
            EventType.LLM_CALL,
            actor="verifier",
            stage=Stage.VERIFY,
            llm=LlmInfo(model="v", tokens_in=800, tokens_out=80, tokens_cached=0),
        ),
        _ev(7, EventType.REWORK, data={"rework": 1}),
        _ev(
            8,
            EventType.GATE_RESOLVED,
            stage=Stage.G1_WAIT,
            human=HumanInfo(who="human:P1", gate="G1", choice="approve", wait_sec=12),
        ),
        _ev(9, EventType.ISSUE_DONE, stage=Stage.DONE),
    ]
    st = derive_role_stats({ISSUE: evs})
    dev = st["dev"]
    assert dev.tasks == 1 and dev.calls == 2 and dev.tokens_in == 1500 and dev.tokens_out == 150
    assert dev.tool_calls == 1 and dev.tool_denied == 1 and dev.tools == {"fs.edit": 1, "exec.run": 1}
    assert dev.fails == 1 and dev.passes == 1 and dev.pass_rate == 0.5 and dev.cache_hit == 600 / 1500
    assert st["verifier"].passes == 1 and st["leader"].passes == 1 and st["planner"].passes == 1
    j = role_stats_json(st)
    assert j["roles"]["dev"]["tok_per_task"] == 1650 and j["totals"]["tool_denied"] == 1
    assert j["roles"]["dev"]["latency_ms_avg"] == 450 and j["roles"]["intake"]["pass_rate"] is None


def _run_to_done(rt: Runtime, fake_clock: FakeClock) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    from digital_development_team.runner.stages import StageRegistry
    from digital_development_team.runner.stages.stub import stub_handlers

    reg = default_handlers(stub=True)
    reg = StageRegistry(
        {**{s: reg.get(s) for s in reg.stages()}, **stub_handlers({"intake": {"risk_tier": "T2"}})}
    )
    runner = Runner(rt.services(with_agents=False), handlers=reg, stub=True)
    rep = runner.run(ISSUE, cancel=CancelToken())
    assert rep.stage is Stage.G1_WAIT, rep
    svc = GateService(rt.db, fake_clock)
    og = svc.open_for(ISSUE, GateId.G1)
    assert og is not None
    fake_clock.advance(17)
    svc.resolve(
        og.id, GateDecision(choice="approve", by="human:P1", at=fake_clock.now(), note="đủ bằng chứng")
    )
    assert runner.run(ISSUE, cancel=CancelToken()).status == "done"


def test_handover_markdown_and_report_roles(runtime: Runtime, fake_clock: FakeClock) -> None:
    _run_to_done(runtime, fake_clock)
    h = build_handover(runtime.layout, runtime.db, runtime.store, ISSUE, now=fake_clock.now())
    md = h.markdown
    assert md.startswith(f"# {ISSUE} — ") and "## Acceptance criteria & bằng chứng" in md
    assert "- [x] **AC1**" in md and "**Verifier:** pass" in md
    assert "G1 **approve** by **human:P1**" in md and "(chờ 17s)" in md and "— đủ bằng chứng" in md
    assert "**Tier:** T2" in md and "## Phê duyệt" in md
    assert h.data["gates"][0]["wait_sec"] == 17 and h.data["status"] == "done"
    text = render_report(build_report(runtime.layout, runtime.db))
    assert "ROLES" in text and "dev" in text and "avg wait 17s" in text

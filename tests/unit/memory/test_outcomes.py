"""memory.outcomes — issue kết thúc → role_outcomes · episode · worker_stats · standup; familiarity thật vào confidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.config.loader import load_config
from digital_development_team.config.resources import HOME_OVERRIDES_DIR, overrides_fingerprint
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, IssueStatus, Stage, Tier
from digital_development_team.core.events import Event, HumanInfo
from digital_development_team.core.models import Checkpoint
from digital_development_team.eventlog.reader import read_events
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.memory.outcomes import (
    familiarity,
    judge_roles,
    outcome_summary,
    read_standup,
    record_issue_outcome,
)
from digital_development_team.reporting.perf import build_perf_review, maybe_write_perf
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.gate import GateStage
from digital_development_team.runner.stages.stub import stub_handlers
from digital_development_team.runner.worker import Worker
from tests.conftest import TESTDATA


def _ingest(rt: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )


def _gate_runner(rt: Runtime) -> Runner:
    stubs = stub_handlers()
    reg = default_handlers(stub=True)
    merged = {**{s: reg.get(s) for s in reg.stages()}, **stubs, Stage.GATE: GateStage(stubs[Stage.GATE])}
    return Runner(rt.services(with_agents=False), handlers=StageRegistry(merged), stub=True)


def test_issue_end_records_roles_episode_stats_standup(runtime: Runtime) -> None:
    _ingest(runtime)
    for issue in ("DDT-0001", "DDT-0003"):
        assert _gate_runner(runtime).run(issue, cancel=CancelToken()).status == "done"
    rows = runtime.db.all("SELECT * FROM role_outcomes ORDER BY seq_no, role")
    assert {r["issue_id"] for r in rows} == {"DDT-0001", "DDT-0003"}
    first = {r["role"]: r for r in rows if r["issue_id"] == "DDT-0001"}
    assert {"intake", "planner", "analyst", "dev", "verifier", "security", "leader"} <= set(first)
    assert first["dev"]["passed"] == 1 and first["dev"]["module"] == "calc" and first["dev"]["seq_no"] == 1
    assert first["planner"]["passed"] == 1 and first["analyst"]["passed"] == 1
    assert runtime.db.scalar("SELECT COUNT(*) FROM episodes") == 2
    ws = runtime.db.one("SELECT * FROM worker_stats WHERE role='dev' AND module='calc'")
    assert ws is not None and ws["attempts"] == 2 and ws["passes"] == 2
    lines = read_standup(runtime.layout)
    assert len(lines) == 2 and "DDT-0001" in lines[0] and "calc" in lines[0]
    # idempotent: ghi lại cùng issue không thêm gì
    cp = runtime.checkpoints.load("DDT-0001")
    assert cp is not None
    again = record_issue_outcome(
        db=runtime.db,
        layout=runtime.layout,
        checkpoint=cp,
        events=read_events(runtime.layout, "DDT-0001"),
        read_slot=lambda s: {},
        agent_versions={},
        now=runtime.clock.now(),
    )
    assert again is None and runtime.db.scalar("SELECT COUNT(*) FROM episodes") == 2


def test_familiarity_from_real_pass_rate_feeds_confidence(runtime: Runtime) -> None:
    _ingest(runtime)
    assert familiarity(runtime.db, module="calc") is None  # chưa có lần nào
    _gate_runner(runtime).run("DDT-0001", cancel=CancelToken())
    assert familiarity(runtime.db, module="calc") is None  # 1 lần chưa đủ
    _gate_runner(runtime).run("DDT-0003", cancel=CancelToken())
    assert familiarity(runtime.db, module="calc") == 1.0
    _gate_runner(runtime).run("DDT-0002", cancel=CancelToken())
    cp = runtime.checkpoints.load("DDT-0002")
    assert cp is not None and cp.facts["confidence_breakdown"]["familiarity"] == 1.0
    assert cp.facts["confidence"] == pytest.approx(0.81 + 0.15 * 0.6, abs=0.005)


def test_no_learn_keeps_default_familiarity(runtime: Runtime) -> None:
    _ingest(runtime)
    rt = replace(runtime, config=load_config(overrides=[("policy", "no_learn", True)]))
    for issue in ("DDT-0001", "DDT-0003", "DDT-0002"):
        _gate_runner(rt).run(issue, cancel=CancelToken())
    cp = rt.checkpoints.load("DDT-0002")
    assert cp is not None and cp.facts["confidence_breakdown"]["familiarity"] == 0.4


T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


def _ev(seq: int, event: EventType, *, stage: Stage | None = None, **kw: Any) -> Event:
    return Event(
        issue_id="DDT-0009",
        actor="runner",
        event=event,
        stage=stage,
        seq=seq,
        ts=T0 + timedelta(seconds=seq),
        **kw,
    )


def test_judge_roles_human_reject_blames_verifier_security_leader(fake_clock: FakeClock) -> None:
    stages = [
        Stage.TRIAGE,
        Stage.LOCALIZE,
        Stage.SPEC,
        Stage.IMPLEMENT,
        Stage.VERIFY,
        Stage.REVIEW,
        Stage.GATE,
    ]
    evs = [_ev(i, EventType.STAGE_ENTER, stage=s) for i, s in enumerate(stages)]
    note = "lộ secret, lỗi bảo mật"
    evs.append(
        _ev(
            20,
            EventType.GATE_RESOLVED,
            human=HumanInfo(gate="G1", choice="reject", who="human:P1", note=note),
        )
    )
    evs.append(_ev(21, EventType.REWORK, data={"reason": "G1 reject"}))
    cp = Checkpoint(
        issue_id="DDT-0009",
        stage=Stage.DONE,
        phase="ready",
        status=IssueStatus.DONE,
        tier=Tier.T2,
        rework=1,
        saved_at=fake_clock.now(),
    )
    slots: dict[str, dict[str, Any]] = {
        "intake.yaml": {"risk_tier": "T1", "type": "bug", "affected_area": "api"},
        "evidence.json": {"criteria": [{"id": "AC1", "result": "pass"}, {"id": "AC2", "result": "unclear"}]},
        "sec_report.json": {"verdict": "pass"},
        "decision.json": {"action": "merge"},
    }
    roles = judge_roles(cp, evs, lambda s: slots.get(s, {}))
    assert roles["intake"].passed is True  # T1 → T2 lệch 1 bậc vẫn chấp nhận
    assert roles["planner"].passed is True
    assert roles["analyst"].passed is False and "AC2" in roles["analyst"].reason
    assert roles["dev"].passed is False and roles["verifier"].passed is False
    assert roles["security"].passed is False and roles["leader"].passed is False


def _seed(rt: Runtime, n: int) -> None:
    """n issue giả: Dev v1 cho 5 issue đầu (pass 2/5), v2 sau đó (pass hết); module auth khó."""
    with rt.db.transaction() as c:
        for i in range(1, n + 1):
            ver = 1 if i <= 5 else 2
            passed = (i in (2, 4)) if i <= 5 else True
            module = "auth" if i in (3, 5) else "calc"
            for role, p in (("dev", passed), ("verifier", True)):
                c.execute(
                    "INSERT INTO role_outcomes(issue_id, role, agent_version, issue_type, module, tier, outcome, passed, reason, tokens, cost_usd, calls, model, seq_no, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"DDT-{i:04d}",
                        role,
                        ver,
                        "bug",
                        module,
                        "T1",
                        "done",
                        int(p),
                        "",
                        1000 * (12 - i),
                        0.0,
                        1,
                        "m",
                        i,
                        "t",
                    ),
                )
            c.execute(
                "INSERT INTO episodes(issue_id, issue_type, module, files, approach, outcome, rework_count, tokens,"
                " wall_sec, human_correction, confidence, evidence_count, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"DDT-{i:04d}",
                    "bug",
                    module,
                    "[]",
                    "",
                    "pass",
                    0 if passed else 1,
                    1000,
                    60,
                    0,
                    0.8,
                    1,
                    "t",
                ),
            )


def test_outcome_summary_and_perf_review(runtime: Runtime, fake_clock: FakeClock) -> None:
    _seed(runtime, 10)
    s = outcome_summary(runtime.db)
    dev = [v for v in s["versions"] if v["role"] == "dev"]
    assert [(v["version"], v["issues"], v["pass_rate"]) for v in dev] == [(1, 5, 0.4), (2, 5, 1.0)]
    auth = next(m for m in s["modules"] if m["module"] == "auth")
    assert auth["hard"] is True and auth["measured"] == 2
    assert s["segments"][0]["range"] == "1-5" and s["segments"][0]["pass1"] == 0.4
    assert s["segments"][2]["range"] == "16+" and s["segments"][2]["issues"] == 0
    md = build_perf_review(runtime.db, upto=10, every=5)
    assert "So sánh phiên bản JD" in md and "| dev | v2 | 5 | 100% |" in md and "**auth**" in md
    assert maybe_write_perf(runtime.layout, runtime.db, 9, now=fake_clock.now()) is None
    p = maybe_write_perf(runtime.layout, runtime.db, 10, now=fake_clock.now())
    assert p is not None and p.name == "perf_010.md" and "sau 10 issue" in p.read_text(encoding="utf-8")


def test_worker_rebuilds_runtime_when_jd_override_changes(tmp_home: Path, fake_clock: FakeClock) -> None:
    rt = build_runtime(tmp_home, clock=fake_clock)
    try:
        _ingest(rt)
        built: list[Runtime] = []

        def make(cfg: object = None) -> Runner:
            live = rt.reloaded(rt.config)
            built.append(live)
            return Runner(live.services(with_agents=True), handlers=default_handlers(stub=True), stub=True)

        w = Worker(
            make(),
            rt.services(with_agents=False),
            poll_s=0.01,
            assets_fingerprint=lambda: overrides_fingerprint(tmp_home),
            rebuild=make,
        )
        assert w.tick(CancelToken()) == "ran:DDT-0001:done"
        assert len(built) == 1
        rel = "agents/04-dev.agent.md"
        text = rt.resolver.read_text(rel).replace("version: 1", "version: 2", 1) + "\nGhi chú thêm cho Dev.\n"
        override = tmp_home / HOME_OVERRIDES_DIR / rel
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(text, encoding="utf-8")
        nxt = w.tick(CancelToken())
        assert nxt.startswith("ran:") and nxt.endswith(":done")
        assert len(built) == 2 and built[1].resolver.find(rel).layer == "home"
        issue = nxt.split(":")[1]
        reloads = [e for e in read_events(rt.layout, issue) if e.event is EventType.CONFIG_RELOADED]
        assert len(reloads) == 1
        assert reloads[0].data["changed"] == ["assets"] and reloads[0].data["rebuilt"] is True
        rows = rt.db.all("SELECT agent_version FROM role_outcomes WHERE role='dev' ORDER BY seq_no")
        assert [int(x["agent_version"]) for x in rows] == [1, 2]
        w.tick(CancelToken())  # không đổi gì → không dựng lại
        assert len(built) == 2
    finally:
        rt.close()


def test_cli_perf_backfill_and_write(runtime: Runtime) -> None:
    from typer.testing import CliRunner

    from digital_development_team.cli.main import app

    _ingest(runtime)
    for issue in ("DDT-0001", "DDT-0003"):
        _gate_runner(runtime).run(issue, cancel=CancelToken())
    with runtime.db.transaction() as c:  # giả lập home cũ: issue đã xong nhưng chưa có số liệu
        c.execute("DELETE FROM role_outcomes")
        c.execute("DELETE FROM episodes")
    cli = CliRunner()
    base = ["--home", str(runtime.home)]
    r = cli.invoke(app, [*base, "perf"])
    assert r.exit_code == 1 and "--backfill" in r.output
    r = cli.invoke(app, [*base, "perf", "--backfill", "--write"])
    assert r.exit_code == 0, r.output
    assert "backfill 2 issue" in r.output and "Performance review sau 2 issue" in r.output
    assert (runtime.layout.reports_dir / "perf_002.md").is_file()
    vers = {int(x["agent_version"]) for x in runtime.db.all("SELECT agent_version FROM role_outcomes")}
    assert vers == {0}
    r = cli.invoke(app, [*base, "perf", "--backfill"])  # lần hai không ghi thêm
    assert "backfill 0 issue" in r.output

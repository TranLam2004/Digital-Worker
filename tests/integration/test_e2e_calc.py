"""E2E Phase 1: ingest calc.csv → 7 stage thật (mock golden) → G1 (conf 0.77 → T2) → approve → MERGE → DONE.

Chốt: main có commit ddt(DDT-0001), pytest trong repo pass (5), worktree xoá, seq liền, events đủ, replay/report chạy.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from digital_development_team.app import build_runtime
from digital_development_team.cli.main import app
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, GateId, IssueStatus, Stage
from digital_development_team.core.models import GateDecision
from digital_development_team.eventlog.reader import read_events
from digital_development_team.eventlog.replay import replay
from digital_development_team.gates import GateService
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.reporting import build_report, render_report
from digital_development_team.runner import Runner, default_handlers
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0001"
GOLDEN = TESTDATA / "golden" / "calc_mean"
REQUIRED_EVENTS = {
    EventType.INGESTED,
    EventType.STAGE_ENTER,
    EventType.STAGE_EXIT,
    EventType.LLM_CALL,
    EventType.TOOL_CALL,
    EventType.SLOT_WRITTEN,
    EventType.WORKTREE_CREATED,
    EventType.CHECKLIST_RUN,
    EventType.GATE_WAIT,
    EventType.GATE_RESOLVED,
    EventType.MERGED,
    EventType.WORKTREE_REMOVED,
    EventType.ISSUE_DONE,
}


def test_e2e_issue_to_commit_on_main(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    git, repo = make_repo(tmp_path)
    base_head = git.head()
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN)
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )

    rep = Runner(rt.services(), handlers=default_handlers(stub=False)).run(ISSUE, cancel=CancelToken())
    assert rep.status == "waiting_gate" and rep.stage is Stage.G1_WAIT, rep
    svc = GateService(rt.db, rt.clock)
    og = svc.open_for(ISSUE, GateId.G1)
    assert og is not None
    svc.resolve(og.id, GateDecision(choice="approve", by="human:P1", at=rt.clock.now(), note="ok"))

    rep2 = Runner(rt.services(), handlers=default_handlers(stub=False)).run(ISSUE, cancel=CancelToken())
    assert rep2.status == "done" and rep2.exit_code == 0, rep2

    # main có commit, code đã sửa, test pass trong repo
    assert git.head() != base_head
    assert git.log(n=1)[0]["message"].startswith(f"ddt({ISSUE})")
    assert "raise ValueError" in (repo / "calc.py").read_text(encoding="utf-8")
    assert "def test_mean_empty" in (repo / "tests" / "test_calc.py").read_text(encoding="utf-8")
    assert not git.is_dirty() and git.current_branch() == "main"
    assert not rt.layout.worktree_dir(ISSUE).exists() and not git.branch_exists(f"ddt/{ISSUE}")
    from digital_development_team.sandbox import build_sandbox
    from digital_development_team.sandbox.profiles import profile_for
    from digital_development_team.tools.exec import read_output

    sb = build_sandbox(rt.config.policy.sandbox, scratch=tmp_path / "scratch2")
    res = sb.run(
        "pytest -q",
        cwd=repo,
        profile=profile_for(rt.config.policy.sandbox, "verifier", repo),
        timeout_s=120,
        cancel=CancelToken(),
    )
    out, _ = read_output(res)
    assert res.exit_code == 0 and "5 passed" in out, out

    # checkpoint · queue · events
    cp = rt.checkpoints.load(ISSUE)
    assert cp is not None and cp.status is IssueStatus.DONE and cp.tier.value == "T2" and cp.worktree is None
    assert (
        cp.facts["merge"]["conflict"] is False and cp.facts["merge_gate"]["status"] == "skipped"
    )  # main không đi tiếp → không chạy lại regression
    assert Queue(rt.db, rt.clock).get(ISSUE).status == "done"  # type: ignore[union-attr]
    sha = cp.facts["merge"]["sha"]
    assert len(sha) == 40  # gitRevision gửi Game Master là SHA đầy đủ
    cache = (rt.layout.memory_dir / "baseline_cache.json").read_text(encoding="utf-8")
    assert f'"{sha}|' in cache  # MERGE nạp sẵn baseline cho main mới → issue kế không chạy lại regression
    evs = read_events(rt.layout, ISSUE)
    types = {e.event for e in evs}
    assert types >= REQUIRED_EVENTS, REQUIRED_EVENTS - types
    assert [e.seq for e in evs] == list(range(len(evs)))
    merged = next(e for e in evs if e.event is EventType.MERGED)
    assert merged.data["sha"] == git.head()[:12] and merged.data["into"] == "main"
    # mọi slot validate lại pass
    for ref in rt.store.list(ISSUE):
        art = rt.store.read(ISSUE, ref.slot, version=ref.version)
        if ref.slot != "patch/":
            rt.registry.validate_slot(ref.slot, art.content)
    # replay + report
    lines = list(replay(evs))
    assert (len(lines) == len(evs) and lines[-1].endswith("issue_done")) or "issue_done" in lines[-1]
    text = render_report(build_report(rt.layout, rt.db))
    assert "done 1" in text and "T2 1" in text and "G1:approve 1" in text and "llm calls" in text
    rt.close()


def test_e2e_cli(tmp_home: Path, tmp_path: Path) -> None:
    _, repo = make_repo(tmp_path)
    cli = CliRunner()
    base = ["--home", str(tmp_home), "--repo", str(repo), "--provider", "mock"]
    assert cli.invoke(app, [*base, "ingest", str(TESTDATA / "backlogs" / "calc.csv")]).exit_code == 0
    r = cli.invoke(app, [*base, "run", "--next", "--golden", str(GOLDEN)])
    assert r.exit_code == 4 and "G1_WAIT" in r.output, r.output
    r = cli.invoke(app, [*base, "gates"])
    assert r.exit_code == 0 and "G1" in r.output
    r = cli.invoke(app, [*base, "resolve", ISSUE, "G1", "--approve", "--by", "human:P1"])
    assert r.exit_code == 0, r.output
    r = cli.invoke(app, [*base, "run", ISSUE, "--golden", str(GOLDEN)])
    assert r.exit_code == 0 and "done @ DONE" in r.output, r.output
    r = cli.invoke(app, [*base, "replay", ISSUE, "--until", "5"])
    assert r.exit_code == 0 and "seq   0" in r.output and "seq   5" in r.output and "seq   6" not in r.output
    r = cli.invoke(app, [*base, "report"])
    assert r.exit_code == 0 and "done 1" in r.output
    r = cli.invoke(app, [*base, "board"])
    assert "done" in r.output

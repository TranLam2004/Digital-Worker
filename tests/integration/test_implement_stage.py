"""IMPLEMENT thật (AgentLoop + mock golden) trên mini_calc; các stage khác stub. Walking skeleton có Dev thật."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from digital_development_team.app import build_runtime
from digital_development_team.cli.main import app
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.eventlog.reader import read_events
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.agent_stage import AgentStage
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0001"
GOLDEN = TESTDATA / "golden" / "calc_mean"


def test_real_implement_with_stub_rest(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    _, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN)
    svc = rt.services()
    reg = default_handlers(stub=True).with_(IMPLEMENT=AgentStage(Stage.IMPLEMENT, "dev"))
    assert isinstance(reg, StageRegistry)
    rep = Runner(svc, handlers=reg, stub=True).run(ISSUE, cancel=CancelToken())
    assert rep.status == "done", rep
    cp = rt.checkpoints.load(ISSUE)
    assert cp is not None and cp.worktree and cp.branch == f"ddt/{ISSUE}" and cp.base_commit
    patch = rt.store.read(ISSUE, "patch/").content
    assert "raise ValueError" in patch["diff"] and cp.usage.tokens > 0
    evs = read_events(rt.layout, ISSUE)
    types = [e.event for e in evs]
    for t in (
        EventType.WORKTREE_CREATED,
        EventType.LLM_CALL,
        EventType.TOOL_CALL,
        EventType.SLOT_WRITTEN,
        EventType.ISSUE_DONE,
    ):
        assert t in types, t
    assert [e.seq for e in evs] == list(range(len(evs)))
    impl_exit = next(e for e in evs if e.event is EventType.STAGE_EXIT and e.stage is Stage.IMPLEMENT)
    assert impl_exit.data["result"]["outcome"]["kind"] == "artifact" and impl_exit.data["tokens"] > 0
    # worktree còn đó (merge thật ở B12), test trong worktree pass với guard mới
    wt = Path(cp.worktree)
    assert (wt / "calc.py").read_text(encoding="utf-8").count("raise ValueError") == 2
    rt.close()


def test_cli_run_real_implement_blocks_without_verify_handlers(tmp_home: Path, tmp_path: Path) -> None:
    _, repo = make_repo(tmp_path)
    cli = CliRunner()
    base = ["--home", str(tmp_home), "--repo", str(repo), "--provider", "mock"]
    assert cli.invoke(app, [*base, "ingest", str(TESTDATA / "backlogs" / "calc.csv")]).exit_code == 0
    # không stub → chạy thật tới GATE; conf 0.77 → T2 → chờ người ở G1 (exit 4)
    r = cli.invoke(app, [*base, "run", ISSUE, "--golden", str(GOLDEN)])
    assert r.exit_code == 4 and "G1_WAIT" in r.output, r.output

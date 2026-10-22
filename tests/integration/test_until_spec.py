"""B10: ingest CSV → TRIAGE (Intake, final_message) → LOCALIZE (Planner, tool_write trên repo) → SPEC (Analyst) với mock golden."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from digital_development_team.app import build_runtime
from digital_development_team.cli.main import app
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.eventlog.reader import read_events
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0001"
GOLDEN = TESTDATA / "golden" / "calc_mean"


def test_until_spec_three_valid_artifacts(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    _, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN)
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    rep = Runner(rt.services(), handlers=default_handlers(stub=False)).run(
        ISSUE, cancel=CancelToken(), until=Stage.SPEC
    )
    assert rep.status == "running" and rep.stage is Stage.SPEC, rep
    intake = rt.store.read(ISSUE, "intake.yaml").content
    plan = rt.store.read(ISSUE, "plan.yaml").content
    spec = rt.store.read(ISSUE, "spec.yaml").content
    assert intake["type"] == "bug" and intake["entry_hints"][0] == "mean"
    assert plan["candidates"][0]["files"][0]["path"] == "calc.py" and plan["localize_method"] == [
        "ripgrep",
        "skeleton",
        "full_read",
    ]
    assert (
        spec["allowed_files"] == ["calc.py", "tests/test_calc.py"] and len(spec["acceptance_criteria"]) == 3
    )
    for sid, data in (("intake", intake), ("plan", plan), ("spec", spec)):
        rt.registry.raise_if_invalid(sid, data)
    cp = rt.checkpoints.load(ISSUE)
    assert (
        cp is not None and cp.tier.value == "T1" and cp.worktree is None
    )  # chưa cần worktree trước IMPLEMENT
    evs = read_events(rt.layout, ISSUE)
    tool_names = [e.tool.name for e in evs if e.tool]
    assert (
        tool_names[:3] == ["fs.search", "fs.list", "fs.read"] and tool_names[-1] == "blackboard.write"
    )  # Planner đọc repo thật
    search_ev = next(e for e in evs if e.tool and e.tool.name == "fs.search")
    assert search_ev.tool.status == "ok"
    assert [e.event for e in evs].count(EventType.LLM_CALL) == 5  # intake 1 · planner 3 · analyst 1
    assert [e.seq for e in evs] == list(range(len(evs)))
    rt.close()


def test_cli_until_spec(tmp_home: Path, tmp_path: Path) -> None:
    _, repo = make_repo(tmp_path)
    cli = CliRunner()
    base = ["--home", str(tmp_home), "--repo", str(repo), "--provider", "mock"]
    assert cli.invoke(app, [*base, "ingest", str(TESTDATA / "backlogs" / "calc.csv")]).exit_code == 0
    r = cli.invoke(app, [*base, "run", ISSUE, "--until", "SPEC", "--golden", str(GOLDEN)])
    assert r.exit_code == 0 and "running @ SPEC" in r.output, r.output
    r = cli.invoke(app, [*base, "debug", "list-slots", ISSUE])
    assert "spec.yaml" in r.output and "plan.yaml" in r.output

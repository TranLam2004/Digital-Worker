from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.blackboard import reindex
from digital_development_team.cli.main import app
from digital_development_team.core.context import Actor, FakeClock
from digital_development_team.core.enums import IssueStatus, Stage, Tier
from digital_development_team.core.errors import CheckpointError
from digital_development_team.core.models import Checkpoint
from digital_development_team.storage import Database
from tests.unit.blackboard.test_store import INTAKE, ISSUE, SPEC


def _cp(issue: str = ISSUE, **kw: object) -> Checkpoint:
    base: dict[str, object] = dict(
        issue_id=issue,
        stage=Stage.TRIAGE,
        phase="ready",
        status=IssueStatus.RUNNING,
        tier=Tier.T1,
        saved_at="2026-10-03T09:00:00Z",
    )
    base.update(kw)
    return Checkpoint.model_validate(base)


def test_checkpoint_save_load_board(runtime: Runtime) -> None:
    cps = runtime.checkpoints
    assert cps.load(ISSUE) is None
    cps.save(_cp(), title="mean([]) crash")
    cps.save(_cp(stage=Stage.IMPLEMENT, phase="stage_done", rework=1, last_seq=7))
    cp = cps.load(ISSUE)
    assert cp is not None and cp.stage is Stage.IMPLEMENT and cp.rework == 1 and cp.last_seq == 7
    cps.save(_cp("DDT-0002", status=IssueStatus.DONE, stage=Stage.DONE))
    rows = cps.board()
    assert [(r.issue_id, r.stage, r.title) for r in rows] == [
        (ISSUE, "IMPLEMENT", "mean([]) crash"),
        ("DDT-0002", "DONE", None),
    ]
    assert [r.issue_id for r in cps.board(status=IssueStatus.DONE)] == ["DDT-0002"]


def test_checkpoint_corrupt_raises(runtime: Runtime) -> None:
    p = runtime.layout.checkpoint_path(ISSUE)
    p.parent.mkdir(parents=True)
    p.write_text('{"issue_id": "DDT-0001", "stage": "NOPE"}', encoding="utf-8")
    with pytest.raises(CheckpointError):
        runtime.checkpoints.load(ISSUE)


def test_reindex_rebuilds_from_fs_and_events(tmp_home: Path, fake_clock: FakeClock) -> None:
    rt = build_runtime(tmp_home, clock=fake_clock)
    rt.store.write(ISSUE, "intake.yaml", INTAKE, actor=Actor(role="intake"))
    rt.store.write(ISSUE, "spec.yaml", SPEC, actor=Actor(role="analyst"))
    rt.store.write(ISSUE, "spec.yaml", {**SPEC, "goal": "v2"}, actor=Actor(role="analyst", sub_id="x"))
    rt.checkpoints.save(_cp(stage=Stage.SPEC))
    before = {(r.slot, r.version, r.written_by, r.sha256) for r in rt.store.list(ISSUE)}
    rt.close()

    # mất DB
    for p in tmp_home.glob("ddt.db*"):
        p.unlink()
    db = Database(tmp_home / "ddt.db")
    rt2 = build_runtime(tmp_home, clock=fake_clock)
    assert rt2.store.list(ISSUE) == []
    rep = reindex(rt2.layout, rt2.db, rt2.registry)
    assert rep.issues == 1 and rep.slots == 3 and rep.from_events == 3 and rep.schema_fail == []
    after = {(r.slot, r.version, r.written_by, r.sha256) for r in rt2.store.list(ISSUE)}
    assert after == before
    assert rt2.store.read(ISSUE, "spec.yaml").content["goal"] == "v2"
    assert [r.stage for r in rt2.checkpoints.board()] == ["SPEC"]
    # seq counter tiếp nối, không trùng
    assert rep.seq_reset[ISSUE] == 4  # 3 slot_written + 1 slot_versioned
    assert rt2.db.peek_seq(ISSUE) == 4
    db.close()
    rt2.close()


def test_reindex_flags_schema_fail_without_events(tmp_home: Path, fake_clock: FakeClock) -> None:
    rt = build_runtime(tmp_home, clock=fake_clock)
    d = rt.layout.issue_dir("DDT-0009")
    d.mkdir(parents=True)
    (d / "intake.v1.yaml").write_text("issue_id: DDT-0009\n", encoding="utf-8")  # thiếu field
    rep = reindex(rt.layout, rt.db, rt.registry)
    assert rep.slots == 1 and rep.from_events == 0 and len(rep.schema_fail) == 1
    ref = rt.store.latest("DDT-0009", "intake.yaml")
    assert ref is not None and ref.written_by == "reindex"
    rt.close()


def test_cli_debug_roundtrip(tmp_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    f = tmp_path / "intake.yaml"
    import yaml

    f.write_text(yaml.safe_dump(INTAKE), encoding="utf-8")
    base = ["--home", str(tmp_home)]
    r = runner.invoke(app, [*base, "debug", "write-slot", ISSUE, "intake.yaml", str(f), "--as", "intake"])
    assert r.exit_code == 0, r.output
    assert f"blackboard://{ISSUE}/intake.yaml@1" in r.output
    r = runner.invoke(app, [*base, "debug", "write-slot", ISSUE, "intake.yaml", str(f), "--as", "dev"])
    assert r.exit_code == 2 and "không được ghi" in r.output
    r = runner.invoke(app, [*base, "debug", "read-slot", ISSUE, "intake.yaml"])
    assert r.exit_code == 0 and "affected_area: calc" in r.output
    r = runner.invoke(app, [*base, "debug", "list-slots", ISSUE])
    assert r.exit_code == 0 and "intake.yaml" in r.output
    r = runner.invoke(app, [*base, "watch", ISSUE, "--no-follow"])
    assert r.exit_code == 0 and "slot_written" in r.output
    r = runner.invoke(app, [*base, "debug", "emit", ISSUE, "stop_requested", "--data", '{"by":"P1"}'])
    assert r.exit_code == 0 and '"seq":1' in r.output
    r = runner.invoke(app, [*base, "debug", "reindex"])
    assert r.exit_code == 0 and "1 issue" in r.output
    r = runner.invoke(app, [*base, "board"])
    assert r.exit_code == 0

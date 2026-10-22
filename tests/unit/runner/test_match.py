"""Thi đấu: xếp lịch · answer theo template · intake theo đề · MatchRunner đồng bộ trang match · E2E nộp bài thật."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.core.context import FakeClock
from digital_development_team.gamemaster import FakeGameMaster, GradeStatus
from digital_development_team.ingest.queue import Queue
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.match import build_answer, read_match
from digital_development_team.runner.match_runner import MatchRunner
from digital_development_team.runner.schedule import Candidate, pick, plan
from digital_development_team.runner.stages.contest import contest_intake
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

SCENARIO = TESTDATA / "matches" / "practice_calc.json"
GOLDEN = TESTDATA / "golden" / "calc_mean"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


# ───── xếp lịch ─────
def test_plan_puts_pi_first_then_urgent_then_value_and_skips_hopeless() -> None:
    cands = [
        Candidate("DDT-0001", "TASK", 400, NOW + timedelta(minutes=30)),
        Candidate("DDT-0002", "BUG", 200, NOW + timedelta(minutes=30)),
        Candidate("DDT-0003", "CR", 100, NOW + timedelta(minutes=12)),  # sắp hết hạn mà còn kịp
        Candidate("DDT-0004", "PI", 100, NOW + timedelta(minutes=15)),
        Candidate("DDT-0005", "CR", 300, NOW + timedelta(minutes=5)),  # không kịp
    ]
    p = plan(cands, now=NOW, margin_s=90)
    assert p.order == ("DDT-0004", "DDT-0003", "DDT-0001", "DDT-0002")
    assert [i for i, _ in p.skip] == ["DDT-0005"]


def test_pick_reserves_pi_slot() -> None:
    order = ["DDT-0001", "DDT-0002", "DDT-0003"]
    types = {"DDT-0001": "BUG", "DDT-0002": "PI", "DDT-0003": "CR"}
    assert pick(order, types, free_general=1, free_pi=1) == ["DDT-0001", "DDT-0002"]
    assert pick(order, types, free_general=0, free_pi=1) == ["DDT-0002"]  # slot PI không nhận BUG/CR


# ───── answer · intake ─────
def test_build_answer_fills_template_from_artifacts() -> None:
    facts = {
        "summary": "Guard empty list",
        "root_cause": "len 0",
        "evidence": "AC1 pass",
        "files": ["calc.py"],
        "sha": "s",
    }
    out = build_answer(
        {"summary": "", "rootCause": "", "evidence": "", "changedFiles": [], "meta": {"commit": ""}},
        facts,
    )
    assert out == {
        "summary": "Guard empty list",
        "rootCause": "len 0",
        "evidence": "AC1 pass",
        "changedFiles": ["calc.py"],
        "meta": {"commit": "s"},
    }
    from digital_development_team.core.errors import ConfigError

    for template in ({}, {"unknown": ""}, {"summary": 1}, {"evidence": [{}]}):
        with pytest.raises(ConfigError):
            build_answer(template, facts)


def test_contest_intake_fixes_type_and_human_routes() -> None:
    intake = {
        "type": "question",
        "route": "human_first",
        "requires_human_before_code": True,
        "risk_tier": "T1",
    }
    assert contest_intake(intake, "BUG") == {
        **intake,
        "type": "bug",
        "route": "standard",
        "requires_human_before_code": False,
    }
    assert contest_intake({"type": "production_incident", "route": "standard"}, "PI") is None
    task = contest_intake({"type": "security_incident", "route": "security"}, "TASK")
    assert task == {"type": "change_request", "route": "standard"}


# ───── MatchRunner (stub stages) ─────
@pytest.fixture
def stub_match(
    tmp_home: Path, fake_clock: FakeClock
) -> Iterator[tuple[Runtime, FakeGameMaster, MatchRunner]]:
    rt = build_runtime(tmp_home, clock=fake_clock, flags=["--match"])
    gm = FakeGameMaster.from_file(SCENARIO, clock=fake_clock)
    gm.start_match()
    svc = replace(rt.services(with_agents=False), gamemaster=gm)
    mr = MatchRunner(
        svc, gm, lambda: Runner(svc, handlers=default_handlers(stub=True), stub=True), slots=1, pi_slot=True
    )
    yield rt, gm, mr
    mr.shutdown()
    rt.close()


def _rows(rt: Runtime) -> dict[str, tuple[str, str]]:
    return {r.source_id: (r.issue_id, r.status) for r in Queue(rt.db, rt.clock).list()}


def test_sync_new_change_and_close(
    stub_match: tuple[Runtime, FakeGameMaster, MatchRunner], fake_clock: FakeClock
) -> None:
    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    rows = _rows(rt)
    assert list(rows) == ["BUG-01"]
    m = read_match(rt.services(with_agents=False), rows["BUG-01"][0])
    assert (
        m["type"] == "BUG"
        and m["points"] == 200
        and m["deadline_at"]
        and m["application_brief"]["name"] == "mini_calc"
    )
    fake_clock.advance(301)
    mr.sync(gm.snapshot())
    assert "CR-01" in _rows(rt)
    fake_clock.advance(300)  # đề CR-01 đổi lúc 600 s, chưa nộp → phiên bản 2, bản 1 superseded
    mr.sync(gm.snapshot())
    rows = _rows(rt)
    assert rows["CR-01"][1] == "superseded" and rows["CR-01@v2"][1] == "queued"
    fake_clock.advance(25 * 60)  # BUG-01 quá hạn → CLOSED → bỏ
    mr.sync(gm.snapshot())
    assert _rows(rt)["BUG-01"][1] == "blocked"


def test_match_over_cancels_and_exits(
    stub_match: tuple[Runtime, FakeGameMaster, MatchRunner], fake_clock: FakeClock
) -> None:
    _, _, mr = stub_match
    fake_clock.advance(40 * 60)
    assert mr.tick() == "over"


def test_start_failure_pauses_at_ingest(stub_match, monkeypatch) -> None:
    from digital_development_team.core.context import CancelToken
    from digital_development_team.gamemaster import GameMasterError

    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    issue = next(iter(_rows(rt).values()))[0]

    def fail(item_id: str):
        raise GameMasterError("temporary outage", status=503)

    monkeypatch.setattr(gm, "start_working", fail)
    svc = replace(rt.services(with_agents=False), gamemaster=gm)
    report = Runner(svc, handlers=default_handlers(stub=True), stub=True).run(issue, cancel=CancelToken())
    assert report.status == "paused"
    assert rt.checkpoints.load(issue).stage.value == "INGEST"
    assert not any(call[0] == "submit" for call in gm.calls)


def test_retry_after_lost_receipt_keeps_original_request(stub_match, monkeypatch) -> None:
    from digital_development_team.gamemaster import GameMasterError
    from digital_development_team.runner.match import start_working, submit
    from digital_development_team.storage.submissions import SubmissionLedger

    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    issue = next(iter(_rows(rt).values()))[0]
    svc = replace(rt.services(with_agents=False), gamemaster=gm)
    start_working(svc, issue)
    original = {"git_revision": "a" * 40, "answer": {"summary": "original"}, "idempotency_key": "fixed"}
    ledger = SubmissionLedger(rt.db)
    ledger.reserve(gm.match_id, "BUG-01", issue, original)
    real_submit = gm.submit
    sent = []

    def lost_receipt(item_id: str, **kwargs):
        sent.append(kwargs)
        result = real_submit(item_id, **kwargs)
        if len(sent) == 1:
            raise GameMasterError("response lost")
        return result

    monkeypatch.setattr(gm, "submit", lost_receipt)
    assert submit(svc, issue, "b" * 40)["status"] == "UNKNOWN"
    assert submit(svc, issue, "c" * 40)["status"] == "ACCEPTED"
    assert sent == [original, original]
    assert rt.store.read(issue, "match.json").content["submission"]["status"] == "ACCEPTED"
    submit(svc, issue, "d" * 40)
    assert len(sent) == 2


def test_pause_cancels_active_work_and_prevents_dispatch(stub_match) -> None:
    from concurrent.futures import Future

    from digital_development_team.core.context import CancelToken

    _, gm, mr = stub_match
    token = CancelToken()
    mr._running["DDT-9999"] = (Future(), token, False)
    gm.pause()
    assert mr.tick() == "paused"
    assert token.is_cancelled() and mr.report.dispatched == 0


def test_stale_snapshot_cancels_active_work(stub_match, fake_clock, monkeypatch) -> None:
    from concurrent.futures import Future

    from digital_development_team.core.context import CancelToken
    from digital_development_team.gamemaster import GameMasterError

    _, gm, mr = stub_match
    token = CancelToken()
    mr._running["DDT-9999"] = (Future(), token, False)

    def fail():
        raise GameMasterError("offline")

    monkeypatch.setattr(gm, "snapshot", fail)
    fake_clock.advance(31)
    assert mr.tick() == "gm_error" and token.is_cancelled()


def test_finished_match_drains_late_grades_without_new_submission(
    stub_match, fake_clock, monkeypatch
) -> None:
    from digital_development_team.gamemaster.models import GradeResult
    from digital_development_team.storage.submissions import SubmissionLedger

    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    issue = next(iter(_rows(rt).values()))[0]
    ledger = SubmissionLedger(rt.db)
    ledger.reserve(
        gm.match_id, "BUG-01", issue, {"git_revision": "a" * 40, "answer": {}, "idempotency_key": "k"}
    )
    ledger.record(gm.match_id, "BUG-01", {"status": "ACCEPTED", "submission_id": "S", "grade": "PENDING"})
    grades = iter([GradeStatus.PENDING, GradeStatus.PASSED])

    def grade(item_id, submission_id):
        return GradeResult(item_id=item_id, submission_id=submission_id, status=next(grades), score=200)

    monkeypatch.setattr(gm, "submission", grade)
    fake_clock.advance(40 * 60)
    assert mr.tick() == "grading"
    fake_clock.advance(11)
    assert mr.tick() == "over"
    assert ledger.get(gm.match_id, "BUG-01")["grade"] == "PASSED"
    assert mr.report.dispatched == 0
    assert not any(call[0] == "submit" for call in gm.calls)


def test_match_end_caps_item_deadline(stub_match, fake_clock) -> None:
    rt, gm, mr = stub_match
    end = fake_clock.now() + timedelta(minutes=5)
    mr.sync(gm.snapshot().model_copy(update={"ends_at": end}))
    issue = next(iter(_rows(rt).values()))[0]
    assert read_match(rt.services(with_agents=False), issue)["deadline_at"] == end.isoformat()


def test_budget_stop_prevents_new_dispatch(stub_match, monkeypatch) -> None:
    _, _, mr = stub_match
    monkeypatch.setattr(
        "digital_development_team.budget.session.session_status", lambda *a: {"level": "stop"}
    )
    assert mr.tick() == "ok"
    assert mr.report.dispatched == 0 and not mr._running


def test_unverified_sha_is_never_submitted(stub_match) -> None:
    from digital_development_team.core.errors import ConfigError
    from digital_development_team.runner.match import submit

    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    issue = next(iter(_rows(rt).values()))[0]
    svc = replace(rt.services(with_agents=False), gamemaster=gm)
    with pytest.raises(ConfigError, match="final-tree"):
        submit(svc, issue, "a" * 40)
    assert not any(call[0] == "submit" for call in gm.calls)


def test_missing_remote_blocks_contest_push(stub_match) -> None:
    from types import SimpleNamespace

    from digital_development_team.runner.stages.merge import MergeStage

    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    issue = next(iter(_rows(rt).values()))[0]
    svc = replace(
        rt.services(with_agents=False),
        gamemaster=gm,
        worktrees=SimpleNamespace(git=SimpleNamespace(has_remote=lambda _: False)),
    )
    ctx = SimpleNamespace(svc=svc, config=rt.config)
    assert MergeStage._push(SimpleNamespace(issue_id=issue), ctx, "main")["push"]["ok"] is False


def test_ambiguous_submission_freezes_item_version(stub_match, fake_clock) -> None:
    from digital_development_team.storage.submissions import SubmissionLedger

    rt, gm, mr = stub_match
    fake_clock.advance(301)
    mr.sync(gm.snapshot())
    issue = _rows(rt)["CR-01"][0]
    SubmissionLedger(rt.db).reserve(
        gm.match_id, "CR-01", issue, {"git_revision": "a" * 40, "answer": {}, "idempotency_key": "k"}
    )
    fake_clock.advance(300)
    mr.sync(gm.snapshot())
    assert "CR-01@v2" not in _rows(rt)


def test_worker_and_scheduler_updates_do_not_overwrite_each_other(stub_match) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from digital_development_team.runner.match import write_match

    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    issue = _rows(rt)["BUG-01"][0]
    svc = rt.services(with_agents=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: write_match(svc, issue, {f"signal_{i}": i}), range(8)))
    final = read_match(svc, issue)
    assert all(final[f"signal_{i}"] == i for i in range(8))


def test_late_grader_is_bounded_by_grace_period(stub_match, fake_clock, monkeypatch) -> None:
    from digital_development_team.gamemaster.models import GradeResult
    from digital_development_team.storage.submissions import SubmissionLedger

    rt, gm, mr = stub_match
    mr.sync(gm.snapshot())
    issue = _rows(rt)["BUG-01"][0]
    ledger = SubmissionLedger(rt.db)
    ledger.reserve(
        gm.match_id, "BUG-01", issue, {"git_revision": "a" * 40, "answer": {}, "idempotency_key": "k"}
    )
    ledger.record(gm.match_id, "BUG-01", {"status": "ACCEPTED", "submission_id": "S", "grade": "PENDING"})
    monkeypatch.setattr(
        gm, "submission", lambda i, s: GradeResult(item_id=i, submission_id=s, status=GradeStatus.PENDING)
    )
    fake_clock.advance(2400)
    assert mr.tick() == "grading"
    fake_clock.advance(301)
    assert mr.tick() == "over" and mr.report.events["grade_timeout"] == 1
    assert ledger.get(gm.match_id, "BUG-01")["grade"] == "PENDING"


# ───── E2E: pipeline thật (mock golden) → merge → push → submit → Grader chạy test ẩn ─────
def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_e2e_match_bug_is_fixed_pushed_submitted_and_graded(
    tmp_home: Path, tmp_path: Path, fake_clock: FakeClock
) -> None:
    _, repo = make_repo(tmp_path)
    remote = tmp_path / "team.git"
    _git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    rt = build_runtime(
        tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN, flags=["--match"]
    )
    gm = FakeGameMaster.from_file(SCENARIO, clock=fake_clock, team_repo=repo)
    gm.start_match()
    svc = replace(rt.services(), gamemaster=gm)
    mr = MatchRunner(
        svc,
        gm,
        lambda: Runner(svc, handlers=default_handlers(stub=False)),
        slots=1,
        pi_slot=False,
        grade_poll_s=0,
    )
    try:
        issue = None
        for _ in range(300):
            mr.tick()
            rows = _rows(rt)
            issue = rows.get("BUG-01", (None, ""))[0]
            sub = read_match(svc, issue).get("submission") if issue else None
            if sub and sub.get("grade") in ("PASSED", "FAILED"):
                break
            time.sleep(0.2)
        assert issue is not None
        m = read_match(svc, issue)
        sub = m["submission"]
        assert m["started"] and len(m["mvs"]) == 2
        assert sub["status"] == "ACCEPTED" and sub["grade"] == GradeStatus.PASSED.value, "\n".join(
            [str(sub), *(log for logs in gm.grader_logs.values() for log in logs)]
        )
        assert set(sub["answer"]) == {"summary", "rootCause", "evidence", "changedFiles"}
        assert sub["answer"]["changedFiles"]  # điền từ patch/
        pushed = subprocess.run(
            ["git", "rev-parse", "main"], cwd=remote, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert pushed == sub["git_revision"] and len(pushed) == 40
        calls = [c for c in gm.calls if c[0] in ("start_working", "submit")]
        assert calls[0] == ("start_working", "BUG-01") and ("submit", "BUG-01") in calls
        assert not rt.db.all("SELECT 1 FROM gate_requests WHERE status='open'")  # không cổng nào chờ người
    finally:
        mr.shutdown()
        rt.close()

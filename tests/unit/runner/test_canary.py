"""Mutation canary: tiêm lỗi tất định vào dòng vừa đổi, Verifier chạy, khôi phục, đo bắt được, chạy lại trên code thật."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import Actor, CancelToken, FakeClock
from digital_development_team.core.enums import EventType, Stage, Tier
from digital_development_team.core.models import StageResult
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.memory.canary_stats import canary_summary, catch_rate
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages import stub_data as sd
from digital_development_team.runner.stages.canary import MUTATIONS, CanaryVerify, added_lines, plan_canary
from digital_development_team.runner.state import IssueState
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0001"
FIX = '    if not xs:\n        raise ValueError("mean of empty list")\n'


def test_mutations() -> None:
    assert MUTATIONS["flip_condition"]("    if n == 0:") == "    if n != 0:"
    assert MUTATIONS["off_by_one"]("    mid = n // 2") == "    mid = n // 3"
    assert MUTATIONS["remove_null_check"]("    if not xs:") == "    if False:"
    assert MUTATIONS["swap_return"]("    return True") == "    return False"
    assert MUTATIONS["delete_assert"]("    assert x > 0").startswith("    pass")
    assert MUTATIONS["off_by_one"]('    raise ValueError("x 1")') is None  # không đụng chuỗi


def test_plan_skips_tests_and_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text(
        "def mean(xs):\n" + FIX + "    return sum(xs) / len(xs)\n", encoding="utf-8"
    )
    diff = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,4 @@\n def mean(xs):\n+    if not xs:\n"
        '+        raise ValueError("mean of empty list")\n--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n'
        "@@ -1 +1,2 @@\n+    assert mean([2]) == 2\n"
    )
    assert all(f == "calc.py" for f, _ in added_lines(diff))
    p1 = plan_canary(ISSUE, 1, diff, tmp_path, ["remove_null_check", "flip_condition"])
    p2 = plan_canary(ISSUE, 1, diff, tmp_path, ["remove_null_check", "flip_condition"])
    assert p1 == p2 and p1 is not None and p1.file == "calc.py" and p1.original == "    if not xs:"


class _FakeVerifier:
    """Như Verifier chạy test: thấy dòng bị đổi khác bản commit → fail, ngược lại pass."""

    stage = Stage.VERIFY
    uses_llm = True
    role = "verifier"

    def __init__(self, wt: Path, committed: str, *, blind: bool = False) -> None:
        self._wt, self._committed, self._blind = wt, committed, blind
        self.calls = 0

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        self.calls += 1
        now = (self._wt / "calc.py").read_text(encoding="utf-8")
        verdict = "pass" if self._blind or now == self._committed else "fail"
        ev = sd.stub_evidence(state.issue_id, {"verdict": verdict}, version=self.calls)
        ctx.svc.store.write(state.issue_id, "evidence.json", ev, actor=Actor(role="verifier"))
        return StageResult(
            stage=Stage.VERIFY, facts={"outcome": {"kind": "artifact"}, "evidence": {"verdict": verdict}}
        )


def _setup(
    tmp_home: Path, tmp_path: Path, fake_clock: FakeClock, *, blind: bool
) -> tuple[Runtime, StageContext, _FakeVerifier, Path]:
    git, repo = make_repo(tmp_path)
    calc = repo / "calc.py"
    text = calc.read_text(encoding="utf-8").replace(
        "def mean(xs: list[float]) -> float:\n", "def mean(xs: list[float]) -> float:\n" + FIX
    )
    calc.write_text(text, encoding="utf-8")
    base = git.head()
    git.commit(message="fix mean")
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo)
    rt = replace(rt, config=load_config(overrides=[("policy", "canary.rate", 1.0)]))
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    diff = git.diff(base=base, cwd=repo)
    rt.store.write(
        ISSUE,
        "patch/",
        {
            "diff": diff,
            "files_touched": ["calc.py"],
            "impl_log": "fix",
            "base_commit": base,
            "head_commit": git.head(),
        },
        actor=Actor(role="dev"),
    )
    svc = rt.services()
    state = IssueState.new(ISSUE)
    state.worktree, state.tier, state.stage = str(repo), Tier.T1, Stage.VERIFY
    ctx = StageContext(
        svc=svc,
        config=rt.config,
        state=state,
        cancel=CancelToken(),
        clock=fake_clock,
        gates=GateService(rt.db, fake_clock),
        budget=None,
    )
    return rt, ctx, _FakeVerifier(repo, text, blind=blind), repo


def test_canary_caught_then_real_verify(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    rt, ctx, fake, repo = _setup(tmp_home, tmp_path, fake_clock, blind=False)
    try:
        res = CanaryVerify(fake).run(ctx.state, ctx)
        assert fake.calls == 2  # lần canary + lần thật
        assert res.facts["canary"]["caught"] is True and res.facts["evidence"]["verdict"] == "pass"
        assert "if not xs:" in (repo / "calc.py").read_text(encoding="utf-8")  # đã khôi phục
        evs = [e.event for e in read_events(rt.layout, ISSUE)]
        assert EventType.CANARY_INJECTED in evs and EventType.CANARY_RESULT in evs
        assert canary_summary(rt.db)["shown"] == 1 and catch_rate(rt.db) == (None, 1)  # chưa đủ 3 lần
    finally:
        rt.close()


def test_blind_verifier_misses_canary(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    rt, ctx, fake, _ = _setup(tmp_home, tmp_path, fake_clock, blind=True)
    try:
        res = CanaryVerify(fake).run(ctx.state, ctx)
        assert res.facts["canary"]["caught"] is False
        row = rt.db.one("SELECT caught, verdict FROM canary_results")
        assert row is not None and row["caught"] == 0 and row["verdict"] == "pass"
        # không phải lần canary (rate 0) → chạy y nguyên một lần
        ctx2 = replace(ctx, config=load_config(overrides=[("policy", "canary.rate", 0.0)]))
        fake.calls = 0
        ctx2.state.facts.pop("canary", None)
        CanaryVerify(fake).run(ctx2.state, ctx2)
        assert fake.calls == 1
    finally:
        rt.close()

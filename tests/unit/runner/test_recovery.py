"""Phase 3: thang phục hồi (runner/recovery.py), blocked_report.json, mở lại issue BLOCKED (runner/reopen.py)."""

from __future__ import annotations

from typing import Any

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import EventType, IssueStatus, Stage
from digital_development_team.core.models import StageResult
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.control import ControlApplier
from digital_development_team.runner.recovery import category_of, choose, classify_failure
from digital_development_team.runner.reopen import reopen_issue
from digital_development_team.runner.state import IssueState
from digital_development_team.storage.control import ControlQueue
from tests.conftest import TESTDATA

ISSUE = "DDT-0001"


def _cls(stage: Stage, reason: str = "", facts: dict[str, Any] | None = None, **kw: Any) -> str:
    return classify_failure(
        from_stage=stage,
        reason=reason,
        facts=facts or {},
        read_slot=kw.get("slots", {}).get,
        denials=kw.get("denials", ()),
    )


def test_classification_is_deterministic() -> None:
    assert _cls(Stage.MACHINE_GATE, facts={"machine_gate": {"scope_violation": ["x.py"]}}) == "scope"
    assert _cls(Stage.MACHINE_GATE, facts={"machine_gate": {"any_fail": True}}) == "test_fail"
    assert _cls(Stage.VERIFY, slots={"evidence.json": {"verdict": "fail"}}) == "test_fail"
    assert _cls(Stage.VERIFY, facts={"outcome": {"kind": "blocked"}}) == "verifier_unavailable"
    assert _cls(Stage.MERGE, facts={"merge": {"conflict": True}}) == "base_moved"
    assert _cls(Stage.IMPLEMENT, facts={"preflight": {"status": "environment"}}) == "environment"
    assert _cls(Stage.IMPLEMENT, facts={"preflight": {"status": "bad_spec"}}) == "test_command"
    assert _cls(Stage.IMPLEMENT, "BUDGET: issue budget hết (tokens)") == "budget_issue"
    assert _cls(Stage.IMPLEMENT, "hết budget stage (turns)") == "budget_stage"
    assert _cls(Stage.IMPLEMENT, "TOO_MANY_ASKS: dev hỏi quá 2 lần") == "needs_decision"
    assert _cls(Stage.IMPLEMENT, "agent kết thúc mà không ghi patch/") == "no_output"
    assert _cls(Stage.SPEC, "requirement is ambiguous: two readings") == "needs_info"
    assert _cls(Stage.IMPLEMENT, "cannot do it", denials=["secret files"]) == "policy_denied"
    assert (
        _cls(Stage.IMPLEMENT, "cannot do it", denials=["scope violation — outside spec.allowed_files"])
        == "scope"
    )
    assert _cls(Stage.G0_WAIT, facts={"gate": {"choice": "reject"}}) == "human_abort"
    assert _cls(Stage.G1_WAIT, facts={"gate": {"choice": "reject", "by": "human:P1"}}) == "test_fail"
    assert _cls(Stage.LOCALIZE, "gave up") == "agent_gave_up"
    assert category_of("test_fail") == "capability" and category_of("environment") == "environment"


def test_ladder_order_limits_and_model_config() -> None:
    cfg = load_config().policy.recovery
    roles = list(load_config().models.roles)
    p = choose(cfg, failure_class="test_fail", from_stage=Stage.VERIFY, counts={}, model_roles=roles)
    assert p is not None and (p.rung, p.to) == ("re_localize", Stage.LOCALIZE)
    p = choose(
        cfg, failure_class="test_fail", from_stage=Stage.VERIFY, counts={"re_localize": 1}, model_roles=roles
    )
    assert p is not None and (p.rung, p.to, p.model_role) == ("model_upgrade", Stage.IMPLEMENT, "dev_strong")
    assert (
        choose(
            cfg,
            failure_class="test_fail",
            from_stage=Stage.VERIFY,
            counts={"re_localize": 1, "model_upgrade": 1},
            model_roles=roles,
        )
        is None
    )
    # planner_strong chưa cấu hình → bậc model_upgrade không áp cho LOCALIZE
    p = choose(
        cfg,
        failure_class="agent_gave_up",
        from_stage=Stage.LOCALIZE,
        counts={"retry_informed": 1},
        model_roles=roles,
    )
    assert p is None
    for cls in ("environment", "policy_denied", "needs_info", "human_abort", "cancelled", "needs_decision"):
        assert (
            choose(cfg, failure_class=cls, from_stage=Stage.IMPLEMENT, counts={}, model_roles=roles) is None
        )


class _FlakyLocalize:
    """LOCALIZE: lần đầu agent bỏ cuộc (hoặc cần thông tin), sau đó dùng stub."""

    stage = Stage.LOCALIZE
    uses_llm = True
    role = "planner"

    def __init__(self, inner: Any, reason: str, times: int = 1) -> None:
        self._inner, self.reason, self.times = inner, reason, times
        self.seen: list[Any] = []

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        self.seen.append(state.facts.get("retry_feedback"))
        if len(self.seen) <= self.times:
            return StageResult(
                stage=self.stage, facts={"outcome": {"kind": "blocked", "reason": self.reason}}
            )
        return self._inner.run(state, ctx)  # type: ignore[no-any-return]


def _ingest(rt: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )


def test_gave_up_stage_is_retried_with_a_recovery_note(runtime: Runtime) -> None:
    _ingest(runtime)
    base = default_handlers(stub=True)
    flaky = _FlakyLocalize(base.get(Stage.LOCALIZE), "could not decide where to start")
    rep = Runner(runtime.services(), handlers=base.with_(LOCALIZE=flaky), stub=True).run(
        ISSUE, cancel=CancelToken()
    )
    assert rep.status == "done" and len(flaky.seen) == 2
    note = flaky.seen[1]
    assert note["to"] == "LOCALIZE" and "recovery step `retry_informed` after `agent_gave_up`" in note["text"]
    rec = [e.data for e in read_events(runtime.layout, ISSUE) if e.event is EventType.RECOVERY]
    assert [r["rung"] for r in rec] == ["retry_informed"]


def test_needs_human_blocks_with_report_then_reopen_continues(runtime: Runtime) -> None:
    _ingest(runtime)
    base = default_handlers(stub=True)
    flaky = _FlakyLocalize(base.get(Stage.LOCALIZE), "the requirement is ambiguous: two readings of 'mean'")
    svc = runtime.services()
    runner = Runner(svc, handlers=base.with_(LOCALIZE=flaky), stub=True)
    rep = runner.run(ISSUE, cancel=CancelToken())
    assert rep.status == "blocked" and len(flaky.seen) == 1  # needs_info: không có bậc nào, báo người ngay
    report = runtime.store.read(ISSUE, "blocked_report.json")
    assert report.ref.written_by == "runner"
    content = report.content
    assert content["category"] == "needs_info" and content["stage"] == "LOCALIZE" and content["resumable"]
    blocked = [e for e in read_events(runtime.layout, ISSUE) if e.event is EventType.BLOCKED][-1]
    assert blocked.data["category"] == "needs_info" and blocked.artifact_ref == report.ref.uri

    # mở lại qua hàng lệnh worker (web gửi) → PAUSED ở LOCALIZE, agent thấy câu trả lời → chạy tiếp tới DONE
    ControlQueue(runtime.db).submit(
        "reopen",
        by="human:P1",
        at=runtime.clock.now(),
        issue=ISSUE,
        payload={"note": "mean = arithmetic mean"},
    )
    retried: list[str] = []
    res = ControlApplier(
        svc,
        gates=GateService(runtime.db, runtime.clock),
        queue=Queue(runtime.db, runtime.clock),
        on_retry=retried.append,
    ).apply_all()
    assert res.applied == 1 and res.failed == 0 and retried == [ISSUE]
    cp = runtime.checkpoints.load(ISSUE)
    assert (
        cp is not None
        and cp.status is IssueStatus.PAUSED
        and cp.stage is Stage.LOCALIZE
        and cp.facts["reopens"] == 1
    )
    assert cp.facts["human_answers"][-1]["note"] == "mean = arithmetic mean"
    assert any(e.event is EventType.REOPENED for e in read_events(runtime.layout, ISSUE))
    rep2 = runner.run(ISSUE, cancel=CancelToken())
    assert rep2.status == "done" and "BLOCKED at LOCALIZE" in flaky.seen[1]["text"]
    # kết quả ghi theo lần kết thúc CUỐI: lần BLOCKED cũ thành superseded, role_outcomes là của lần DONE
    outs = [
        r["outcome"]
        for r in runtime.db.all("SELECT outcome FROM episodes WHERE issue_id=? ORDER BY id", (ISSUE,))
    ]
    assert outs == ["superseded", "pass"]
    assert {
        r["outcome"] for r in runtime.db.all("SELECT outcome FROM role_outcomes WHERE issue_id=?", (ISSUE,))
    } == {"done"}
    ok, err = reopen_issue(svc, runtime.config.policy, ISSUE, by="human:P1")
    assert not ok and "chỉ mở lại được issue BLOCKED" in err["error"]


def test_reopen_respects_the_limit(runtime: Runtime) -> None:
    _ingest(runtime)
    base = default_handlers(stub=True)
    flaky = _FlakyLocalize(base.get(Stage.LOCALIZE), "ambiguous requirement", times=99)
    svc = runtime.services()
    runner = Runner(svc, handlers=base.with_(LOCALIZE=flaky), stub=True)
    policy = runtime.config.policy
    for i in range(policy.recovery.max_reopens):
        assert runner.run(ISSUE, cancel=CancelToken()).status == "blocked"
        ok, _ = reopen_issue(svc, policy, ISSUE, by="human:P1", note=f"try {i}", budget_pct=50)
        assert ok
    assert runner.run(ISSUE, cancel=CancelToken()).status == "blocked"
    report = runtime.store.read(ISSUE, "blocked_report.json").content
    assert not report["resumable"] and report["reopens"] == policy.recovery.max_reopens
    ok, err = reopen_issue(svc, policy, ISSUE, by="human:P1")
    assert not ok and "tối đa" in err["error"]
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.facts["budget_grant_pct"] == 50 * policy.recovery.max_reopens


def test_human_abort_is_final(runtime: Runtime) -> None:
    from digital_development_team.core.enums import GateId
    from digital_development_team.core.models import GateDecision
    from digital_development_team.runner.stages import StageRegistry
    from digital_development_team.runner.stages.stub import stub_handlers

    _ingest(runtime)
    base = default_handlers(stub=True)
    reg = StageRegistry(
        {**{s: base.get(s) for s in base.stages()}, **stub_handlers({"intake": {"risk_tier": "T3"}})}
    )
    runner = Runner(runtime.services(), handlers=reg, stub=True)
    assert runner.run(ISSUE, cancel=CancelToken()).status == "waiting_gate"
    gates = GateService(runtime.db, runtime.clock)
    og = gates.open_for(ISSUE, GateId.G0)
    assert og is not None
    gates.resolve(
        og.id, GateDecision(choice="reject", by="human:P1", at=runtime.clock.now(), note="not worth it")
    )
    assert runner.run(ISSUE, cancel=CancelToken()).status == "blocked"
    assert not [e for e in read_events(runtime.layout, ISSUE) if e.event is EventType.RECOVERY]
    assert runtime.store.read(ISSUE, "blocked_report.json").content["category"] == "cancelled"


def test_cli_reopen(runtime: Runtime) -> None:
    from typer.testing import CliRunner

    from digital_development_team.cli.main import app

    _ingest(runtime)
    base = default_handlers(stub=True)
    flaky = _FlakyLocalize(base.get(Stage.LOCALIZE), "ambiguous requirement")
    assert (
        Runner(runtime.services(), handlers=base.with_(LOCALIZE=flaky), stub=True)
        .run(ISSUE, cancel=CancelToken())
        .status
        == "blocked"
    )
    cli = CliRunner()
    r = cli.invoke(
        app,
        ["--home", str(runtime.home), "reopen", ISSUE, "--note", "use the arithmetic mean", "--budget", "50"],
    )
    assert r.exit_code == 0, r.output
    assert "LOCALIZE" in r.output and "+50% budget" in r.output
    r2 = cli.invoke(app, ["--home", str(runtime.home), "reopen", ISSUE])
    assert r2.exit_code == 2 and "chỉ mở lại được issue BLOCKED" in r2.output

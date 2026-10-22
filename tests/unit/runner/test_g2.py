"""G2 thật: lời gọi tool phá huỷ bị chặn kèm khoá → người duyệt đúng lời gọi đó (một lần / cả issue / từ chối)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from digital_development_team.agents import AgentRegistry
from digital_development_team.app import Runtime, build_runtime, build_tooling
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import Actor, CancelToken, FakeClock, ToolContext
from digital_development_team.core.enums import EventType, GateId, RiskTier, Stage, Tier
from digital_development_team.core.events import EventDraft, ToolInfo
from digital_development_team.core.models import GateDecision, ToolCall
from digital_development_team.gates import GateService
from digital_development_team.gates.decisions import check_choice
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.policy.approvals import ApprovalStore, approval_key
from digital_development_team.policy.engine import PolicyContext
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.stub import stub_handlers
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0001"


def test_engine_issues_key_and_honours_approval(runtime: Runtime, tmp_path: Path) -> None:
    from digital_development_team.policy.engine import PolicyEngine
    from digital_development_team.tools.catalog import ToolCatalog

    cfg = load_config()
    eng = PolicyEngine(cfg.policy, cfg.sensitive_paths, ToolCatalog())
    dev = AgentRegistry().get("dev")
    call = ToolCall(
        id="t",
        name="exec.run",
        args={"cmd": "pytest -q"},
        actor=Actor(role="dev"),
        issue_id=ISSUE,
        stage=Stage.IMPLEMENT,
    )
    ctx = PolicyContext(
        tier=Tier.T1, worktree=str(tmp_path), allowed_files=("calc.py",), autonomy="conservative"
    )
    d = eng.decide(call, dev, ctx)
    key = approval_key("exec.run", call.args, None)
    assert d.action == "deny" and d.gate == "G2" and d.extra["approval_key"] == key
    approved = replace(ctx, approvals={key: "once"})
    d2 = eng.decide(call, dev, approved)
    assert d2.action == "allow" and d2.rule_id == "g2_approved" and d2.extra["scope"] == "once"
    other = ToolCall(
        id="u",
        name="exec.run",
        args={"cmd": "pytest -q -x"},
        actor=Actor(role="dev"),
        issue_id=ISSUE,
        stage=Stage.IMPLEMENT,
    )
    assert eng.decide(other, dev, approved).action == "deny"  # quyền gắn đúng một lời gọi


def test_dispatcher_consumes_once_approval(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    _, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo)
    try:
        tooling = build_tooling(rt, base_ref=lambda c: "main")
        dev = AgentRegistry(rt.resolver).get("dev")
        call = ToolCall(
            id="c",
            name="exec.run",
            args={"cmd": 'python -c "print(1)"'},
            actor=Actor(role="dev"),
            issue_id=ISSUE,
            stage=Stage.IMPLEMENT,
        )
        tctx = ToolContext(
            issue_id=ISSUE,
            actor=Actor(role="dev"),
            stage=Stage.IMPLEMENT,
            worktree=str(repo),
            home=str(rt.home),
            allowed_files=("calc.py",),
            cancel=CancelToken(),
            clock=fake_clock,
        )
        pctx = PolicyContext(
            tier=Tier.T1, worktree=str(repo), allowed_files=("calc.py",), autonomy="conservative"
        )
        r = tooling.dispatcher.dispatch(call, dev, tctx, pctx)
        assert r.status == "denied" and r.structured["approval_key"]
        ApprovalStore(rt.layout).grant(
            ISSUE, key=r.structured["approval_key"], scope="once", by="human:P2", at=fake_clock.now()
        )
        assert tooling.dispatcher.dispatch(call, dev, tctx, pctx).status == "ok"
        assert tooling.dispatcher.dispatch(call, dev, tctx, pctx).status == "denied"  # một lần là hết
    finally:
        rt.close()


def _denial(rt: Runtime, key: str) -> None:
    rt.sink.emit(
        EventDraft(
            issue_id=ISSUE,
            actor="dev",
            event=EventType.TOOL_DENIED,
            stage=Stage.IMPLEMENT,
            tool=ToolInfo(
                name="fs.delete",
                risk_tier=RiskTier.DESTRUCTIVE,
                denied_reason="G2: destructive",
                status="denied",
            ),
            data={
                "rule": "rule#6",
                "layer": 4,
                "path": "migrations/0001.sql",
                "gate": "G2",
                "approval_key": key,
            },
        )
    )


def test_destructive_request_opens_g2_and_allow_grants(runtime: Runtime, fake_clock: FakeClock) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    form = {
        "type": "destructive_request",
        "question": "Cần xoá migration cũ 0001.sql vì nó tạo bảng trùng tên, có được xoá không?",
        "options": [
            {"id": "A", "action": "Xoá migrations/0001.sql", "cost": "1 file", "risk": "high"},
            {"id": "B", "action": "Giữ file, dừng issue", "cost": "0", "risk": "low", "route": "abort"},
        ],
        "recommendation": "A",
    }
    reg = default_handlers(stub=True)
    reg = StageRegistry(
        {**{s: reg.get(s) for s in reg.stages()}, **stub_handlers({"patch": {"escalate": form}})}
    )
    runner = Runner(runtime.services(with_agents=False), handlers=reg, stub=True)
    _denial(runtime, "fs.delete:migrations/0001.sql")
    rep = runner.run(ISSUE, cancel=CancelToken())
    assert rep.status == "waiting_gate" and rep.stage is Stage.G1_WAIT
    gates = GateService(runtime.db, fake_clock)
    og = gates.open_for(ISSUE, GateId.G2)
    assert og is not None and og.ticket.timeout_s is None  # thao tác phá huỷ không có mặc định
    assert og.summary["g2"]["approval_key"] == "fs.delete:migrations/0001.sql"
    check_choice(GateId.G2, "allow_issue", None, options=["A", "B"])
    gates.resolve(og.id, GateDecision(choice="allow_issue", by="human:P1", at=fake_clock.now()))
    assert runner.run(ISSUE, cancel=CancelToken()).status == "done"
    assert ApprovalStore(runtime.layout).active(ISSUE) == {"fs.delete:migrations/0001.sql": "issue"}
    form_v = runtime.store.read(ISSUE, "escalation.yaml").content
    assert (
        form_v["resolved"]["choice"] == "allow_issue" and "được phép" in form_v["resolved"]["chosen_action"]
    )


def test_g2_deny_needs_reason_and_grants_nothing(runtime: Runtime, fake_clock: FakeClock) -> None:
    import pytest

    from digital_development_team.gates.decisions import GateError

    with pytest.raises(GateError, match="cần lý do"):
        check_choice(GateId.G2, "deny", None)
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    form = {
        "type": "destructive_request",
        "question": "Cần xoá migration cũ 0001.sql vì nó tạo bảng trùng tên, có được xoá không?",
        "recommendation": "A",
    }
    reg = default_handlers(stub=True)
    reg = StageRegistry(
        {**{s: reg.get(s) for s in reg.stages()}, **stub_handlers({"patch": {"escalate": form}})}
    )
    runner = Runner(runtime.services(with_agents=False), handlers=reg, stub=True)
    _denial(runtime, "fs.delete:migrations/0001.sql")
    runner.run(ISSUE, cancel=CancelToken())
    og = GateService(runtime.db, fake_clock).open_for(ISSUE, GateId.G2)
    assert og is not None
    GateService(runtime.db, fake_clock).resolve(
        og.id, GateDecision(choice="deny", by="human:P1", at=fake_clock.now(), note="không xoá migration")
    )
    runner.run(ISSUE, cancel=CancelToken())
    assert ApprovalStore(runtime.layout).active(ISSUE) == {}
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and "không được chạy" in cp.facts["human_answers"][-1]["chosen_action"]

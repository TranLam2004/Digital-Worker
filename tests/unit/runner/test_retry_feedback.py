"""Phase 1: retry có thông tin (runner/feedback.py) + vá đường đi (question, VERIFY blocked/escalation, G1 timeout,
merge conflict lên base mới, model rework)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import GateId, Stage
from digital_development_team.core.models import GateDecision, GateTicket, StageResult
from digital_development_team.isolation.worktree import WorktreeManager
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.feedback import build_retry_feedback
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.agent_stage import AgentStage
from digital_development_team.runner.stages.gate_wait import _REOPEN, GateWaitStage
from digital_development_team.runner.stages.parallel import ParallelStages
from digital_development_team.runner.stages.stub import stub_handlers
from digital_development_team.runner.stages.verify_fallback import VerifyFallback
from digital_development_team.runner.state import IssueState
from digital_development_team.runner.transitions import TransitionTable
from digital_development_team.storage import HomeLayout
from tests.unit.isolation.test_git import make_repo
from tests.unit.runner.test_transitions import LIMITS, _state

ISSUE = "DDT-0001"


def _fb(
    from_stage: Stage, facts: dict[str, Any], slots: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    return build_retry_feedback(
        from_stage=from_stage,
        to_stage=Stage.IMPLEMENT,
        facts=facts,
        read_slot=(slots or {}).get,
        read_artifact=lambda _r: "line 1\nE   AssertionError: boom\n",
    )


# ───── khối phản hồi ─────
def test_machine_gate_failure_reaches_dev() -> None:
    fb = _fb(
        Stage.MACHINE_GATE,
        {
            "mg_failures": [
                {
                    "gate": "unit_test",
                    "summary": "1 failed",
                    "cmd": "pytest -q",
                    "failed_tests": [{"name": "tests/t.py::test_a", "message": "assert 1 == 2"}],
                    "output_ref": "artifact://DDT-0001/a.log",
                },
                {"gate": "lint", "summary": "E501", "output_ref": "artifact://DDT-0001/b.log"},
            ],
            "machine_gate": {"scope_violation": ["setup.py"]},
            "checklist": {"blocking_fail": True, "failed_ids": ["TEST-011"]},
        },
    )
    assert fb is not None and fb["from"] == "MACHINE_GATE" and fb["to"] == "IMPLEMENT"
    text = fb["text"]
    assert "unit_test` failed: 1 failed" in text and "FAILED tests/t.py::test_a - assert 1 == 2" in text
    assert "AssertionError: boom" in text  # lint không có test → đuôi log
    assert "setup.py" in text and "TEST-011" in text
    assert fb["refs"] == ["artifact://DDT-0001/a.log", "artifact://DDT-0001/b.log"]


def test_leader_verifier_merge_and_gate_feedback() -> None:
    ev = {
        "verdict": "fail",
        "failure_feedback_for_dev": "calc.py:4 no guard",
        "criteria": [{"id": "AC1", "result": "fail", "evidence": "pytest: 1 failed"}],
    }
    dec = {"action": "rework", "rework_instructions": "use candidate #2"}
    gate_fb = _fb(Stage.GATE, {}, {"decision.json": dec, "evidence.json": ev})
    assert gate_fb is not None
    assert "Leader (rework): use candidate #2" in gate_fb["text"] and "calc.py:4 no guard" in gate_fb["text"]
    merge = {
        "conflict": True,
        "reason": "conflict",
        "files": ["calc.py"],
        "prior_diff_ref": "artifact://x.diff",
    }
    merge_fb = _fb(Stage.MERGE, {"merge": merge})
    assert merge_fb is not None and "artifact://x.diff" in merge_fb["text"] and "calc.py" in merge_fb["text"]
    g1 = {
        "gate": "G1",
        "choice": "reject",
        "note": "missing None test",
        "is_timeout": False,
        "by": "human:P1",
    }
    g1_fb = _fb(Stage.G1_WAIT, {"gate": g1})
    assert g1_fb is not None and "Rejected at G1 by human:P1: missing None test" in g1_fb["text"]
    # câu trả lời escalation không lặp lại (đã có trong human_answers) · không có lý do cụ thể → None
    assert _fb(Stage.G1_WAIT, {"gate": {**g1, "escalation": True}}) is None
    assert _fb(Stage.VERIFY, {}, {"evidence.json": {**ev, "verdict": "pass"}}) is None
    assert (
        build_retry_feedback(
            from_stage=Stage.SPEC,
            to_stage=Stage.IMPLEMENT,
            facts={},
            read_slot=lambda _s: None,
            read_artifact=lambda _r: None,
        )
        is None
    )


class _Capture:
    """Bọc IMPLEMENT: ghi lại retry_feedback mà stage nhìn thấy mỗi lần chạy."""

    stage = Stage.IMPLEMENT
    uses_llm = True
    role = "dev"

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.seen: list[Any] = []

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        self.seen.append(state.facts.get("retry_feedback"))
        return self._inner.run(state, ctx)  # type: ignore[no-any-return]


def test_runner_hands_machine_gate_failure_to_the_retry(runtime: Runtime) -> None:
    from digital_development_team.ingest import Queue, ingest_file
    from tests.conftest import TESTDATA

    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    base = default_handlers(stub=True)
    reg = StageRegistry(
        {**{s: base.get(s) for s in base.stages()}, **stub_handlers({"machine_gate": {"fail_times": 1}})}
    )
    cap = _Capture(reg.get(Stage.IMPLEMENT))
    rep = Runner(runtime.services(), handlers=reg.with_(IMPLEMENT=cap), stub=True).run(
        ISSUE, cancel=CancelToken()
    )
    assert rep.status == "done"
    assert cap.seen[0] is None  # lần đầu không có gì để nói
    second = cap.seen[1]
    assert second["from"] == "MACHINE_GATE" and "test_mean_empty - ZeroDivisionError" in second["text"]
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and "retry_feedback" not in cp.facts  # IMPLEMENT đã dùng xong → bỏ


# ───── transitions ─────
@pytest.fixture(scope="module")
def table() -> TransitionTable:
    return TransitionTable.from_policy(load_config().policy)


def test_question_answered_on_any_route(table: TransitionTable) -> None:
    q = {"type": "question", "risk_tier": "T1", "confidence": 0.9, "route": "standard"}
    assert table.next(Stage.TRIAGE, {"intake": q}).primary is Stage.ANSWER
    low = {**q, "confidence": 0.4}
    assert table.next(Stage.TRIAGE, {"intake": low}).primary is Stage.G0_WAIT  # người xem trước
    gate = {"choice": "approve", "route": "", "return_to": ""}
    assert table.next(Stage.G0_WAIT, {"intake": low, "gate": gate}).primary is Stage.ANSWER


def test_verify_blocked_retries_then_escalation_goes_to_human(table: TransitionTable) -> None:
    def sc(
        kind: str, *, retries: int = 0, review: str | None = None, verdict: str = "fail"
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "outcome": {"kind": kind},
            "evidence": {"verdict": verdict},
            "state": _state(verify_retries=retries),
            "limits": LIMITS,
        }
        if review:
            out["outcomes"] = {"VERIFY": {"kind": kind}, "REVIEW": {"kind": review}}
        return out

    d = table.next(Stage.VERIFY, sc("blocked"))
    assert d.primary is Stage.VERIFY and d.rule.bump == {"verify_retries": "+1"}
    assert table.next(Stage.VERIFY, sc("blocked", retries=1, verdict="inconclusive")).primary is Stage.GATE
    assert table.next(Stage.VERIFY, sc("escalation")).primary is Stage.G1_WAIT
    assert (
        table.next(Stage.VERIFY, sc("artifact", review="escalation", verdict="pass")).primary is Stage.G1_WAIT
    )
    g = {"choice": "A", "route": "continue", "return_to": "VERIFY"}  # escalation: choice = id phương án
    assert table.next(Stage.G1_WAIT, {"gate": g, "state": _state(), "limits": LIMITS}).primary is Stage.VERIFY


# ───── stage ─────
class _Blocked:
    stage = Stage.VERIFY
    uses_llm = True
    role = "verifier"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        self.calls += 1
        return StageResult(
            stage=self.stage, facts={"outcome": {"kind": "blocked", "reason": "no test runner"}}
        )


def test_verifier_blocked_twice_gives_inconclusive_evidence_and_human_gate(runtime: Runtime) -> None:
    from digital_development_team.ingest import Queue, ingest_file
    from tests.conftest import TESTDATA

    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    inner = _Blocked()
    reg = default_handlers(stub=True).with_(VERIFY=VerifyFallback(inner))
    rep = Runner(runtime.services(), handlers=reg, stub=True).run(ISSUE, cancel=CancelToken())
    assert inner.calls == 2 and rep.status == "waiting_gate" and rep.stage is Stage.G1_WAIT
    ev = runtime.store.read(ISSUE, "evidence.json")
    assert ev.ref.written_by == "runner" and ev.content["verdict"] == "inconclusive"
    assert {c["result"] for c in ev.content["criteria"]} == {"unclear"}


class _Out:
    uses_llm = True

    def __init__(self, stage: Stage, kind: str) -> None:
        self.stage, self._kind = stage, kind

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        return StageResult(stage=self.stage, facts={"outcome": {"kind": self._kind}, self.stage.value: 1})


def test_parallel_keeps_each_branch_outcome(runtime: Runtime) -> None:
    group = ParallelStages([_Out(Stage.VERIFY, "artifact"), _Out(Stage.REVIEW, "escalation")])
    ctx = StageContext(
        svc=runtime.services(with_agents=False),
        config=runtime.config,
        state=IssueState.new(ISSUE),
        cancel=CancelToken(),
        clock=runtime.clock,
        gates=None,  # type: ignore[arg-type]
        budget=None,
        stub=True,
    )
    res = group.run(IssueState.new(ISSUE), ctx)
    assert res.facts["outcome"] == {"kind": "artifact"}  # của VERIFY, không phải nhánh xong sau
    assert res.facts["outcomes"] == {"VERIFY": {"kind": "artifact"}, "REVIEW": {"kind": "escalation"}}


def test_g1_timeout_reopens_when_more_code_cannot_help() -> None:
    cfg = load_config().policy.gates.g1
    stage = GateWaitStage(Stage.G1_WAIT, GateId.G1)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    ticket = GateTicket(id="DDT-0001:G1:1", issue_id=ISSUE, gate=GateId.G1, opened_at=t0, timeout_s=300)
    d = GateDecision(choice="timeout", by="timeout", at=t0, is_timeout=True)
    st = IssueState.new(ISSUE)
    st.facts.update({"evidence": {"verdict": "inconclusive"}, "decision": {"action": "merge"}})
    assert _REOPEN in stage._apply(d, cfg, st, ticket)
    st.facts["evidence"] = {"verdict": "fail"}
    facts = stage._apply(d, cfg, st, ticket)
    assert facts["gate"]["choice"] == "reject" and "verifier=fail" in facts["gate"]["note"]


def test_rework_uses_the_stronger_model_role() -> None:
    cfg = load_config()
    stage = AgentStage(Stage.IMPLEMENT, "dev")
    st = IssueState.new(ISSUE)
    assert stage.model_role_for(st, cfg) == "dev"
    st.rework = cfg.policy.rework_model_escalation_at
    assert stage.model_role_for(st, cfg) == "dev_rework"
    assert AgentStage(Stage.VERIFY, "verifier").model_role_for(st, cfg) == "verifier"


def test_merge_conflict_resets_worktree_onto_new_base(tmp_path: Path) -> None:
    g, repo = make_repo(tmp_path)
    wm = WorktreeManager(g, HomeLayout(tmp_path / "home.ddt").ensure())
    info = wm.ensure(ISSUE)
    (info.path / "calc.py").write_text("dev change\n", encoding="utf-8")
    g.commit(cwd=info.path, message="dev")
    (repo / "calc.py").write_text("main change\n", encoding="utf-8")
    g.commit(message="main moved")
    ok, files = g.rebase(cwd=info.path, onto="main")
    assert not ok and files == ["calc.py"]
    new_base, backup = wm.reset_onto(ISSUE, "main", backup_suffix="attempt1")
    assert new_base == g.head() and g.head(cwd=info.path) == new_base
    assert (info.path / "calc.py").read_text(encoding="utf-8") == "main change\n"
    assert backup == f"ddt/{ISSUE}-attempt1" and g.branch_exists(backup)

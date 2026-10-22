"""Phase 5: loại task + profile theo loại (policy.yaml#task_types) + Dev kết luận 'không cần sửa gì' → Leader close."""

from __future__ import annotations

from typing import Any

from digital_development_team.agents.brief import BriefInputs, render_brief
from digital_development_team.app import Runtime
from digital_development_team.budget import resolve_stage_budget
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import Actor
from digital_development_team.core.enums import Stage, Tier
from digital_development_team.llm.mock import MockProvider, ScriptStep
from digital_development_team.runner.transitions import TransitionTable
from tests.unit.agents.test_loop import _run, env  # noqa: F401  (fixture)
from tests.unit.runner.test_transitions import LIMITS, _state

ISSUE = "DDT-0001"


def test_profiles_and_budget_multiplier() -> None:
    policy = load_config().policy
    assert policy.task_profile("refactor").dev[0].startswith("Behaviour must not change")
    assert policy.task_profile("docs").test_command == "optional"
    assert policy.task_profile("no-such-type") == policy.task_profile("default")
    budgets = load_config().budgets
    base = resolve_stage_budget(budgets, Stage.IMPLEMENT, Tier.T1)
    perf = resolve_stage_budget(
        budgets, Stage.IMPLEMENT, Tier.T1, multiplier=policy.task_profile("perf").budget_multiplier
    )
    assert base is not None and perf is not None and perf.turns > base.turns and perf.tokens > base.tokens


def test_brief_constraints_follow_the_task_profile(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    rt, svc = env
    intake = dict(rt.store.read(ISSUE, "intake.yaml").content)
    rt.store.write(ISSUE, "intake.yaml", {**intake, "type": "refactor"}, actor=Actor(role="intake"))
    ref = resolve_stage_budget(rt.config.budgets, Stage.IMPLEMENT, Tier.T1)
    assert ref is not None and svc.agents and svc.brief_builder

    def text(role: str, stage: Stage) -> str:
        spec = svc.agents.get(role)
        b = svc.brief_builder.build(spec, BriefInputs(issue_id=ISSUE, stage=stage, tier=Tier.T1, budget=ref))
        return render_brief(b, rt.resolver)

    dev = text("dev", Stage.IMPLEMENT)
    assert (
        "Task profile (refactor): Behaviour must not change" in dev
        and "Write a failing test first" not in dev
    )
    assert "Task profile (refactor): pass needs" in text("verifier", Stage.VERIFY)
    assert "Task profile (refactor): ACs:" in text("analyst", Stage.SPEC)


def test_new_types_route_and_close_flow() -> None:
    table = TransitionTable.from_policy(load_config().policy)
    spike = {"type": "spike", "risk_tier": "T1", "confidence": 0.9, "route": "standard"}
    assert table.next(Stage.TRIAGE, {"intake": spike}).primary is Stage.ANSWER
    docs = {**spike, "type": "docs"}
    assert table.next(Stage.TRIAGE, {"intake": docs}).primary is Stage.LOCALIZE
    out = {"kind": "artifact", "slot": "answer.yaml"}
    assert (
        table.next(Stage.IMPLEMENT, {"outcome": out, "state": _state(), "limits": LIMITS}).primary
        is Stage.GATE
    )

    def gate(dec: dict[str, Any], tier: str = "T1") -> Stage:
        return table.next(
            Stage.GATE,
            {
                "decision": dec,
                "state": _state(tier=tier),
                "limits": LIMITS,
                "g1_required": tier in ("T2", "T3"),
            },
        ).primary

    assert gate({"action": "close", "requires_human": False}) is Stage.DONE
    assert gate({"action": "close", "requires_human": False}, tier="T2") is Stage.G1_WAIT
    assert gate({"action": "close", "requires_human": True}) is Stage.G1_WAIT
    g1 = {"choice": "approve", "route": "", "return_to": ""}
    sc = {"gate": g1, "decision": {"action": "close"}, "state": _state(), "limits": LIMITS}
    assert table.next(Stage.G1_WAIT, sc).primary is Stage.DONE
    assert table.next(Stage.G1_WAIT, {**sc, "decision": {"action": "merge"}}).primary is Stage.MERGE


def test_dev_can_conclude_no_change_needed(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    rt, svc = env
    answer = {
        "issue_id": ISSUE,
        "answer": "mean([]) already raises ValueError on main: pytest -q tests/test_calc.py::test_mean_empty → 1 passed",
        "references": ["calc.py:3"],
        "confidence": 0.9,
        "resolution": "already_fixed",
    }
    mock = MockProvider(echo=False)
    mock.add_script(
        "dev",
        [
            ScriptStep(
                text="already fixed",
                tool_calls=(
                    {"name": "blackboard_write", "input": {"slot": "answer.yaml", "content": answer}},
                ),
                stop="tool_use",
            ),
            ScriptStep(text="done", stop="end_turn"),
        ],
    )
    outcome, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=None, mock=mock)
    assert outcome.kind == "artifact" and outcome.artifact_uri.startswith(
        f"blackboard://{ISSUE}/answer.yaml@"
    )
    assert rt.store.read(ISSUE, "answer.yaml").content["resolution"] == "already_fixed"


def test_preflight_requires_test_command_by_profile(tmp_home, tmp_path, fake_clock) -> None:  # type: ignore[no-untyped-def]
    from digital_development_team.app import build_runtime
    from digital_development_team.runner.stages.preflight import run_preflight
    from digital_development_team.runner.state import IssueState
    from tests.unit.isolation.test_git import make_repo
    from tests.unit.runner.test_spec_amend import SPEC, _ctx

    _, repo = make_repo(tmp_path)
    rt: Runtime = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock")
    try:
        intake = {
            "issue_id": ISSUE,
            "type": "docs",
            "severity": "P3",
            "complexity": "trivial",
            "risk_tier": "T0",
            "route": "standard",
            "confidence": 0.9,
            "affected_area": "calc",
            "rationale": "README typo.",
        }
        rt.store.write(ISSUE, "intake.yaml", intake, actor=Actor(role="intake"))
        no_cmd = {k: v for k, v in SPEC.items() if k not in ("test_command", "regression_command")}
        rt.store.write(ISSUE, "spec.yaml", no_cmd, actor=Actor(role="analyst"))
        state = IssueState.new(ISSUE)
        assert run_preflight(state, _ctx(rt, state), 1)["status"] == "ok"  # docs: không cần lệnh test
        rt.store.write(ISSUE, "intake.yaml", {**intake, "type": "bug"}, actor=Actor(role="intake"))
        pf = run_preflight(state, _ctx(rt, state), 2)
        assert pf["status"] == "bad_spec" and "required for task type `bug`" in pf["problems"][0]
    finally:
        rt.close()

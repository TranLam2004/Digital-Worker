"""Phase 2: sửa spec giữa chừng (spec_patch) + preflight của IMPLEMENT (lệnh spec, baseline đỏ sẵn)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.config.loader import load_config
from digital_development_team.config.models import SpecAmendCfg
from digital_development_team.core.context import Actor, CancelToken, FakeClock
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.feedback import build_retry_feedback
from digital_development_team.runner.spec_amend import (
    apply_chosen_patch,
    auto_amend_from_escalation,
    auto_approvable,
    check_patch,
)
from digital_development_team.runner.stages.machine_gate import _minus_baseline
from digital_development_team.runner.stages.preflight import run_preflight
from digital_development_team.runner.state import IssueState
from digital_development_team.runner.transitions import TransitionTable
from tests.unit.isolation.test_git import make_repo
from tests.unit.runner.test_transitions import LIMITS, _state

ISSUE = "DDT-0001"
SPEC: dict[str, Any] = {
    "issue_id": ISSUE,
    "goal": "mean([]) raises ValueError",
    "acceptance_criteria": [{"id": "AC1", "text": "mean([]) raises ValueError", "verify_by": "test"}],
    "allowed_files": ["calc.py", "tests/test_calc.py"],
    "test_command": "python -m pytest -q tests/test_calc.py",
    "regression_command": "python -m pytest -q",
    "rollback_plan": "git revert HEAD",
}


@pytest.fixture
def env(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock):  # type: ignore[no-untyped-def]
    g, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock")
    yield rt, g, repo
    rt.close()


def _ctx(rt: Runtime, state: IssueState) -> StageContext:
    return StageContext(
        svc=rt.services(),  # có worktree + sandbox
        config=rt.config,
        state=state,
        cancel=CancelToken(),
        clock=rt.clock,
        gates=GateService(rt.db, rt.clock),
        budget=None,
        stub=True,
    )


def _write_spec(rt: Runtime, **over: Any) -> None:
    rt.store.write(ISSUE, "spec.yaml", {**SPEC, **over}, actor=Actor(role="analyst"))


def _escalation(rt: Runtime, patch: dict[str, Any]) -> None:
    rt.store.write(
        ISSUE,
        "escalation.yaml",
        {
            "issue_id": ISSUE,
            "raised_by": "dev",
            "stage": "IMPLEMENT",
            "type": "ambiguity",
            "question": "The fix needs a new test module outside allowed_files. Allow it?",
            "options": [
                {
                    "id": "A",
                    "action": "Add tests/test_more.py",
                    "cost": "+1 file",
                    "risk": "low",
                    "spec_patch": patch,
                },
                {"id": "B", "action": "Keep everything in tests/test_calc.py", "cost": "0", "risk": "low"},
            ],
            "recommendation": "A",
            "confidence": 0.8,
            "blocked_since": "2026-01-01T00:00:00+00:00",
        },
        actor=Actor(role="dev"),
    )


# ───── quy tắc thuần ─────
def test_patch_checks_and_auto_rules(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    guard = load_config().policy.command_guard
    bad = check_patch(
        {"allowed_files_add": ["../etc/passwd", "nodir/x.py"], "test_command": "curl http://x"},
        SPEC,
        root=tmp_path,
        guard=guard,
        max_files=20,
    )
    assert any("unsafe path" in p for p in bad) and any("parent directory" in p for p in bad)
    assert any("test_command blocked" in p for p in bad)
    assert (
        check_patch(
            {"allowed_files_add": ["tests/test_more.py"]}, SPEC, root=tmp_path, guard=guard, max_files=20
        )
        == []
    )
    too_many = check_patch(
        {"allowed_files_add": ["tests/a.py"]}, SPEC, root=tmp_path, guard=guard, max_files=2
    )
    assert too_many and "exceed 2" in too_many[0]

    cfg = SpecAmendCfg()
    sens = load_config().sensitive_paths.globs()
    ok = {
        "allowed_files_add": ["tests/test_more.py", "calc_helpers.py"]
    }  # file test + cùng thư mục với calc.py
    assert auto_approvable(ok, SPEC, cfg, sensitive_globs=sens, used=0)
    assert not auto_approvable(ok, SPEC, cfg, sensitive_globs=sens, used=1)  # 1 lần/issue
    assert not auto_approvable({**ok, "test_command": "pytest"}, SPEC, cfg, sensitive_globs=sens, used=0)
    assert not auto_approvable(
        {"allowed_files_add": ["lib/far_away.py"]}, SPEC, cfg, sensitive_globs=sens, used=0
    )
    assert not auto_approvable(
        {"allowed_files_add": [".github/workflows/ci.yml"]}, SPEC, cfg, sensitive_globs=sens, used=0
    )


# ───── áp vào pipeline ─────
def test_dev_escalation_with_small_patch_is_auto_approved(env) -> None:  # type: ignore[no-untyped-def]
    rt, _, _ = env
    _write_spec(rt)
    _escalation(rt, {"allowed_files_add": ["tests/test_more.py"]})
    state = IssueState.new(ISSUE)
    ctx = _ctx(rt, state)
    assert auto_amend_from_escalation(state, ctx, Stage.IMPLEMENT)
    spec = rt.store.read(ISSUE, "spec.yaml")
    assert spec.ref.written_by == "runner" and "tests/test_more.py" in spec.content["allowed_files"]
    assert spec.content["amendments"][0]["by"] == "system:auto"
    assert state.facts["human_answers"][-1]["by"] == "system" and state.facts["spec_amend_auto"] == 1
    assert rt.store.read(ISSUE, "escalation.yaml").content["resolved"]["choice"] == "A"
    ev = [e for e in read_events(rt.layout, ISSUE) if e.event is EventType.SPEC_AMENDED]
    assert ev and ev[-1].data["auto"] is True and ev[-1].data["added"] == ["tests/test_more.py"]
    # lần hai trong issue → không tự duyệt nữa (người quyết qua gate)
    _escalation(rt, {"allowed_files_add": ["tests/test_even_more.py"]})
    assert not auto_amend_from_escalation(state, ctx, Stage.IMPLEMENT)


def test_human_choice_applies_command_change(env) -> None:  # type: ignore[no-untyped-def]
    rt, _, _ = env
    _write_spec(rt)
    _escalation(rt, {"test_command": "python -m pytest -q tests"})
    state = IssueState.new(ISSUE)
    esc = dict(rt.store.read(ISSUE, "escalation.yaml").content)
    assert not auto_amend_from_escalation(state, _ctx(rt, state), Stage.IMPLEMENT)  # đổi lệnh: không tự duyệt
    res = apply_chosen_patch(state, _ctx(rt, state), esc, "A", by="human:P1", stage=Stage.IMPLEMENT)
    assert res is not None and res.ok
    assert rt.store.read(ISSUE, "spec.yaml").content["test_command"] == "python -m pytest -q tests"
    assert apply_chosen_patch(state, _ctx(rt, state), esc, "B", by="human:P1", stage=None) is None


# ───── preflight ─────
def test_preflight_catches_bad_paths_missing_programs_and_baseline(env) -> None:  # type: ignore[no-untyped-def]
    rt, g, repo = env
    (repo / "tests" / "test_broken.py").write_text(
        "def test_broken():\n    assert 1 == 2\n", encoding="utf-8"
    )
    g.commit(message="a test that already fails on main")
    state = IssueState.new(ISSUE)
    ctx = _ctx(rt, state)
    _write_spec(
        rt,
        test_command="python -m pytest -q tests/test_new.py",
        allowed_files=["calc.py", "tests/test_new.py"],
    )
    pf = run_preflight(state, ctx, 1)
    assert pf["status"] == "ok" and state.worktree  # file test sẽ do Dev tạo → hợp lệ
    assert pf["baseline_failures"] == ["tests/test_broken.py::test_broken"]
    _write_spec(rt, test_command="python -m pytest -q tests/test_missing.py")
    assert run_preflight(state, ctx, 2)["status"] == "bad_spec"
    _write_spec(rt, test_command="nosuchprog_ddt --run", regression_command="nosuchprog_ddt --all")
    env_pf = run_preflight(state, ctx, 3)
    assert env_pf["status"] == "environment" and "not installed" in env_pf["problems"][0]


def test_machine_gate_ignores_failures_that_were_red_on_base() -> None:
    parsed = {"status": "fail", "failed_tests": [{"name": "t::a"}]}
    g = {"status": "fail", "summary": "1 failed", "parsed": parsed}
    assert _minus_baseline(g, {"t::a", "t::b"})["status"] == "pass"
    assert _minus_baseline(g, {"t::b"})["status"] == "fail"
    assert (
        _minus_baseline({**g, "parsed": {"status": "fail", "failed_tests": []}}, {"t::a"})["status"] == "fail"
    )


# ───── transitions + phản hồi cho Analyst ─────
def test_bad_spec_goes_back_to_analyst_once() -> None:
    table = TransitionTable.from_policy(load_config().policy)
    out = {"kind": "blocked"}
    sc = lambda respec: {  # noqa: E731
        "preflight": {"status": "bad_spec"},
        "outcome": out,
        "state": _state(respec=respec),
        "limits": LIMITS,
    }
    d = table.next(Stage.IMPLEMENT, sc(0))
    assert d.primary is Stage.SPEC and d.rule.bump == {"respec": "+1"}
    assert table.next(Stage.IMPLEMENT, sc(1)).primary is Stage.BLOCKED
    gate = {"choice": "A", "route": "re_spec", "return_to": "IMPLEMENT"}
    assert (
        table.next(Stage.G1_WAIT, {"gate": gate, "state": _state(), "limits": LIMITS}).primary is Stage.SPEC
    )
    fb = build_retry_feedback(
        from_stage=Stage.IMPLEMENT,
        to_stage=Stage.SPEC,
        facts={"preflight": {"status": "bad_spec", "problems": ["test_command: `x.py` does not exist"]}},
        read_slot=lambda _s: None,
        read_artifact=lambda _r: None,
    )
    assert fb is not None and "`x.py` does not exist" in fb["text"]


def test_preflight_baseline_is_cached_per_base_commit(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Cùng commit base + cùng lệnh regression → không chạy lại bộ test trước mỗi lần Dev làm (C2)."""
    from digital_development_team.runner.stages import preflight

    rt, g, repo = env
    (repo / "tests" / "test_broken.py").write_text(
        "def test_broken():\n    assert 1 == 2\n", encoding="utf-8"
    )
    g.commit(message="red on main")
    _write_spec(rt)
    state = IssueState.new(ISSUE)
    first = run_preflight(state, _ctx(rt, state), 1)
    assert first["baseline_failures"] == ["tests/test_broken.py::test_broken"]
    hit = preflight.baseline_cache_get(rt.layout, str(state.base_commit), SPEC["regression_command"])
    assert hit is not None and hit["baseline_failures"] == first["baseline_failures"]
    calls: list[str] = []
    monkeypatch.setattr(preflight, "run_gate_cmd", lambda *a, **k: calls.append("run") or {})
    again = run_preflight(state, _ctx(rt, state), 2)  # spec đổi version, base không đổi
    assert calls == [] and again["baseline_failures"] == first["baseline_failures"]

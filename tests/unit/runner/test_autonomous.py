"""Autonomy `autonomous` (--match): không cổng nào chờ người — thi đấu, người chạm máy = −50 điểm.

Cùng stub stages như test_runner/test_escalation; chỉ đổi config sang --match.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, GateId, Stage, Tier
from digital_development_team.core.models import GateDecision, GateTicket
from digital_development_team.eventlog.reader import read_events
from digital_development_team.llm.cli_session import CliSession
from digital_development_team.risk.confidence import compute_confidence
from digital_development_team.runner import Runner, RunReport, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.gate_wait import AUTO_ACTOR, GateWaitStage
from digital_development_team.runner.stages.stub import stub_handlers
from digital_development_team.runner.state import IssueState
from tests.unit.runner.test_escalation import FORM, _ingest

ISSUE = "DDT-0001"


@pytest.fixture
def match_rt(tmp_home: Path, fake_clock: FakeClock) -> Iterator[Runtime]:
    rt = build_runtime(tmp_home, clock=fake_clock, flags=["--match"])
    _ingest(rt)
    yield rt
    rt.close()


def _runner(rt: Runtime, overrides: Mapping[str, Mapping[str, Any]]) -> Runner:
    reg = default_handlers(stub=True)
    reg = StageRegistry({**{s: reg.get(s) for s in reg.stages()}, **stub_handlers(overrides)})
    return Runner(rt.services(with_agents=False), handlers=reg, stub=True)


def _run(r: Runner) -> RunReport:
    return r.run(ISSUE, cancel=CancelToken())


def _tickets(rt: Runtime) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in rt.db.all("SELECT gate, status, decided_by FROM gate_requests WHERE issue_id=?", (ISSUE,))
    ]


def test_match_flag_sets_autonomous_profile() -> None:
    p = load_config(flags=["--match"]).policy
    assert p.autonomy == "autonomous" and p.auto_decide and p.no_learn and not p.canary.enabled
    assert p.gate_rule_tiers == ["T0", "T1"] and p.effective_conf_proceed() == 0.70
    lo, hi = p.effective_conf_bump_range()
    assert lo == hi  # không có vùng "+1 tier"
    assert not load_config().policy.auto_decide


def test_t3_low_confidence_runs_to_done_without_human(match_rt: Runtime) -> None:
    """Balanced: T3 chờ G0 và G1 mãi. Autonomous: hai ticket mở để lưu vết, harness quyết ngay, tới DONE."""
    rep = _run(_runner(match_rt, {"intake": {"risk_tier": "T3", "confidence": 0.4}}))
    assert rep.status == "done", rep.reason
    tickets = _tickets(match_rt)
    assert {t["gate"] for t in tickets} == {"G0", "G1"}
    assert all(t["status"] == "resolved" and t["decided_by"] == AUTO_ACTOR for t in tickets)
    cp = match_rt.checkpoints.load(ISSUE)
    assert cp is not None and cp.tier is Tier.T3 and not cp.facts.get("conservative")  # không nâng tier


def test_g1_rejects_on_failing_evidence_then_blocks_without_waiting(match_rt: Runtime) -> None:
    rep = _run(_runner(match_rt, {"evidence": {"verdict": "fail"}, "decision": {"requires_human": True}}))
    assert rep.status == "blocked"  # rework hết lượt → bỏ item, không treo chờ người
    assert all(t["status"] == "resolved" for t in _tickets(match_rt))


def test_escalation_takes_agent_recommendation(match_rt: Runtime) -> None:
    rep = _run(_runner(match_rt, {"spec": {"escalate": FORM}}))
    assert rep.status == "done"
    resolved = match_rt.store.read(ISSUE, "escalation.yaml").content["resolved"]
    assert resolved["chosen_action"] == "Giữ v1, thêm adapter" and resolved["by"] == AUTO_ACTOR
    assert not [e for e in read_events(match_rt.layout, ISSUE) if e.event is EventType.HUMAN_OVERRIDE]


def test_auto_choice_for_g1() -> None:
    stage = GateWaitStage(Stage.G1_WAIT, GateId.G1)
    cfg = load_config(flags=["--match"]).policy.gates.g1
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    ticket = GateTicket(id="DDT-0001:G1:1", issue_id=ISSUE, gate=GateId.G1, opened_at=t0, timeout_s=None)
    d = GateDecision(choice="timeout", by=AUTO_ACTOR, at=t0, is_timeout=True)
    st = IssueState.new(ISSUE)
    st.facts.update({"evidence": {"verdict": "inconclusive"}, "decision": {"action": "merge"}})
    facts = stage._apply(d, cfg, st, ticket, auto=True)
    assert facts["gate"]["choice"] == "approve"  # balanced sẽ mở lại ticket chờ người
    st.facts["sec_report"] = {"verdict": "fail"}
    assert stage._apply(d, cfg, st, ticket, auto=True)["gate"]["choice"] == "reject"


def test_brand_new_repo_perfect_t0_clears_threshold() -> None:
    p = load_config(flags=["--match"]).policy
    perfect = {
        "evidence": {"verdict": "pass", "regression": {"result": "pass"}, "criteria": [{"result": "pass"}]},
        "spec": {"ambiguities": []},
        "sec_report": None,
        "rework": 0,
        "reloc": 0,
    }
    preset = p.effective_autonomy()
    assert preset is not None and preset.familiarity_unknown is not None
    conf = compute_confidence(p.confidence_weights, familiarity=preset.familiarity_unknown, **perfect)  # type: ignore[arg-type]
    assert conf.value >= p.effective_conf_proceed()
    balanced = load_config().policy
    old = compute_confidence(balanced.confidence_weights, **perfect)  # type: ignore[arg-type]
    assert old.value < balanced.effective_conf_proceed()  # bẫy cũ: 0.785 < 0.80


def test_mcp_child_rebuilds_same_preset_and_flags(
    tmp_home: Path, fake_clock: FakeClock, tmp_path: Path
) -> None:
    rt = build_runtime(tmp_home, clock=fake_clock, flags=["--match"], provider="codex")
    try:
        assert rt.config.preset == "codex_all" and rt.config.flags == ("--match",)
    finally:
        rt.close()
    sess = CliSession(
        home=str(tmp_home),
        repo=None,
        config_dir=None,
        issue_id=ISSUE,
        role="dev",
        stage="IMPLEMENT",
        tier="T1",
        worktree=None,
        allowed_files=(),
        spec_values={},
        autonomy="autonomous",
        tools=(),
        preset=rt.config.preset,
        flags=rt.config.flags,
    )
    loaded = CliSession.load(sess.dump(tmp_path / "s.json"))
    assert loaded == sess
    child = load_config(flags=loaded.flags, preset=loaded.preset)  # cách cli/mcp_serve dựng runtime
    assert child.policy.autonomy == "autonomous" and child.models.providers["codex_cli"].enabled


def test_past_deadline_blocks_without_recovery(tmp_home: Path, fake_clock: FakeClock) -> None:
    from datetime import timedelta

    from digital_development_team.core.context import Actor

    rt = build_runtime(tmp_home, clock=fake_clock, flags=["--match"])
    try:
        rt.store.write(
            ISSUE,
            "item.json",
            {
                "id": ISSUE,
                "source": "gamemaster",
                "source_id": "BUG-01",
                "title": "API trả sai dữ liệu",
                "body": "x",
                "received_at": fake_clock.now().isoformat(),
                "deadline_at": (fake_clock.now() + timedelta(seconds=30)).isoformat(),  # < biên 90 s
            },
            actor=Actor(role="runner"),
        )
        rep = _run(_runner(rt, {}))
        assert rep.status == "blocked" and "DEADLINE" in (rep.reason or "")
        cp = rt.checkpoints.load(ISSUE)
        assert cp is not None and not (cp.facts.get("recovery") or {}).get("history")
    finally:
        rt.close()


def test_split_disabled_in_match() -> None:
    from digital_development_team.config.loader import load_config as lc

    assert lc().policy.split_enabled and not lc(flags=["--match"]).policy.split_enabled

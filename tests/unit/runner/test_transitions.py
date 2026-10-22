from __future__ import annotations

from typing import Any

import pytest

from digital_development_team.config.loader import load_config
from digital_development_team.config.models import TransitionRule
from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import ConfigError, MissingValue
from digital_development_team.runner.transitions import TransitionTable

LIMITS = {
    "max_rework": 2,
    "max_reloc": 1,
    "machine_gate_free_retries": 2,
    "conf_proceed": 0.8,
    "conf_bump_lo": 0.5,
    "conf_bump_hi": 0.8,
    "conf_escalate": 0.5,
}


@pytest.fixture(scope="module")
def table() -> TransitionTable:
    return TransitionTable.from_policy(load_config().policy)


def _state(**kw: Any) -> dict[str, Any]:
    base = {
        "tier": "T1",
        "rework": 0,
        "reloc": 0,
        "mg_retries": 0,
        "g0_visits": 0,
        "touches_sensitive": False,
    }
    base.update(kw)
    return base


def test_triage_routes(table: TransitionTable) -> None:
    intake = {"type": "bug", "risk_tier": "T1", "confidence": 0.9, "route": "standard"}
    assert table.next(Stage.TRIAGE, {"intake": intake}).primary is Stage.LOCALIZE
    assert table.next(Stage.TRIAGE, {"intake": {**intake, "risk_tier": "T3"}}).primary is Stage.G0_WAIT
    assert table.next(Stage.TRIAGE, {"intake": {**intake, "confidence": 0.5}}).primary is Stage.G0_WAIT
    assert (
        table.next(Stage.TRIAGE, {"intake": {**intake, "requires_human_before_code": True}}).primary
        is Stage.G0_WAIT
    )
    assert (
        table.next(Stage.TRIAGE, {"intake": {**intake, "route": "fast", "type": "question"}}).primary
        is Stage.ANSWER
    )


def test_verify_rework_reloc_blocked(table: TransitionTable) -> None:
    sc = lambda verdict, **st: {"evidence": {"verdict": verdict}, "state": _state(**st), "limits": LIMITS}  # noqa: E731
    assert table.next(Stage.VERIFY, sc("pass")).primary is Stage.GATE
    assert table.next(Stage.VERIFY, sc("inconclusive")).primary is Stage.GATE
    d = table.next(Stage.VERIFY, sc("fail", rework=1))
    assert d.primary is Stage.IMPLEMENT and d.rule.rework == "+1" and d.cancel is Stage.REVIEW
    d2 = table.next(Stage.VERIFY, sc("fail", rework=2, reloc=0))
    assert d2.primary is Stage.LOCALIZE and d2.rule.reloc == "+1"
    assert table.next(Stage.VERIFY, sc("fail", rework=2, reloc=1)).primary is Stage.BLOCKED


def test_machine_gate_and_parallel(table: TransitionTable) -> None:
    ok = {"machine_gate": {"any_fail": False}, "state": _state(), "limits": LIMITS}
    d = table.next(Stage.MACHINE_GATE, ok)
    assert d.to == (Stage.VERIFY, Stage.REVIEW) and d.is_parallel
    fail = {"machine_gate": {"any_fail": True}, "state": _state(mg_retries=1), "limits": LIMITS}
    d = table.next(Stage.MACHINE_GATE, fail)
    assert d.primary is Stage.IMPLEMENT and d.rule.mg_retries == "+1" and d.rule.rework is None
    fail2 = {"machine_gate": {"any_fail": True}, "state": _state(mg_retries=2, rework=2), "limits": LIMITS}
    assert table.next(Stage.MACHINE_GATE, fail2).primary is Stage.BLOCKED


def test_review_skip_t0_and_gate_rule_only(table: TransitionTable) -> None:
    pol = {"review_skip_tiers": ["T0"]}
    sc = {"state": _state(tier="T0"), "limits": LIMITS, "policy": pol}
    assert table.peek_skip(Stage.REVIEW, sc)
    assert not table.peek_skip(Stage.REVIEW, {"state": _state(tier="T1"), "limits": LIMITS, "policy": pol})
    match = {"review_skip_tiers": ["T0", "T1"]}  # --match: T1 sạch cũng bỏ REVIEW
    assert table.peek_skip(Stage.REVIEW, {"state": _state(tier="T1"), "limits": LIMITS, "policy": match})
    gate_t0 = {
        "state": _state(tier="T0"),
        "limits": LIMITS,
        "evidence": {"verdict": "pass"},
        "confidence": 0.9,
    }
    d = table.next(Stage.GATE, gate_t0)
    assert d.primary is Stage.MERGE and d.rule.rule_only
    gate_t1 = {"state": _state(), "limits": LIMITS, "decision": {"action": "merge", "requires_human": True}}
    assert table.next(Stage.GATE, gate_t1).primary is Stage.G1_WAIT
    assert table.next(Stage.GATE, {**gate_t1, "decision": {"action": "supersede"}}).primary is Stage.DONE


def test_missing_value_fails_closed(table: TransitionTable) -> None:
    with pytest.raises(MissingValue):
        table.next(Stage.TRIAGE, {})  # không có intake
    with pytest.raises(MissingValue):
        table.next(Stage.VERIFY, {"evidence": {}, "state": _state(), "limits": LIMITS})  # thiếu verdict


def test_table_requires_default() -> None:
    with pytest.raises(ConfigError, match="default"):
        TransitionTable({"TRIAGE": [TransitionRule(when="x", to="LOCALIZE")]})
    with pytest.raises(ConfigError, match="đích lạ"):
        TransitionTable({"TRIAGE": [TransitionRule(to="NOPE")]})
    assert table_desc()["to"] == "VERIFY+REVIEW"


def table_desc() -> dict[str, Any]:
    t = TransitionTable({"MACHINE_GATE": [TransitionRule(to=["VERIFY", "REVIEW"], note="x")]})
    return t.next(Stage.MACHINE_GATE, {}).describe()

from __future__ import annotations

import pytest

from digital_development_team.core.errors import SchemaError
from digital_development_team.schema import SLOT_SCHEMA, SchemaRegistry, render_contract
from digital_development_team.schema.views import EvidenceView, IntakeView, SpecView

INTAKE_OK = {
    "issue_id": "DDT-0001",
    "type": "bug",
    "severity": "P2",
    "complexity": "small",
    "risk_tier": "T1",
    "route": "standard",
    "confidence": 0.84,
    "affected_area": "calc",
    "entry_hints": ["mean", "ZeroDivisionError"],
    "rationale": "Stack trace rõ, 1 hàm.",
}


@pytest.fixture(scope="module")
def reg() -> SchemaRegistry:
    return SchemaRegistry()


def test_loads_all_schemas(reg: SchemaRegistry) -> None:
    assert set(SLOT_SCHEMA.values()) <= set(reg.ids())
    assert reg.check_all() == []


def test_validate_ok_and_fail(reg: SchemaRegistry) -> None:
    assert reg.is_valid("intake", INTAKE_OK)
    bad = {**INTAKE_OK, "type": "feature", "confidence": 1.5}
    errs = reg.violations("intake", bad)
    assert {e.path for e in errs} >= {"type", "confidence"}
    with pytest.raises(SchemaError) as ei:
        reg.raise_if_invalid("intake", bad)
    assert len(ei.value.brief()) <= 5


def test_additional_properties_rejected(reg: SchemaRegistry) -> None:
    assert not reg.is_valid("intake", {**INTAKE_OK, "extra": 1})


def test_evidence_conditional_required(reg: SchemaRegistry) -> None:
    base = reg.schema("evidence")["examples"][0]
    assert reg.is_valid("evidence", base)
    fail = {**base, "verdict": "fail", "criteria": [{**base["criteria"][0], "result": "fail"}]}
    errs = reg.violations("evidence", fail)
    assert any("failure_feedback_for_dev" in e.message for e in errs)


def test_decision_tier_forces_human(reg: SchemaRegistry) -> None:
    base = reg.schema("decision")["examples"][0]
    bad = {**base, "final_tier": "T3", "requires_human": False}
    assert not reg.is_valid("decision", bad)


def test_views_parse_examples(reg: SchemaRegistry) -> None:
    IntakeView.model_validate(INTAKE_OK)
    ev = EvidenceView.model_validate(reg.schema("evidence")["examples"][0])
    assert ev.verdict == "pass" and len(ev.criteria) == 3
    spec_ex = {
        "issue_id": "DDT-0001",
        "goal": "g",
        "acceptance_criteria": [{"id": "AC1", "text": "t", "verify_by": "test", "test_ref": "x"}],
        "allowed_files": ["a.py", "tests/test_a.py"],
        "test_command": "pytest -q",
        "rollback_plan": "git revert HEAD",
    }
    assert reg.is_valid("spec", spec_ex)
    assert SpecView.model_validate(spec_ex).acceptance_criteria[0].verify_by == "test"


def test_item_schema_from_issue(reg: SchemaRegistry) -> None:
    ex = reg.schema("item")["examples"][0]
    assert reg.is_valid("item", ex)
    assert not reg.is_valid("item", {**ex, "id": "X-1"})


def test_event_schema_examples(reg: SchemaRegistry) -> None:
    for ex in reg.schema("event")["examples"]:
        assert reg.is_valid("event", ex), ex


def test_render_contract(reg: SchemaRegistry) -> None:
    txt = render_contract(reg.schema("spec"), "spec.yaml")
    assert "spec.yaml" in txt
    assert "*acceptance_criteria" in txt
    assert "issue_id:" not in txt and "harness fills: amendments, issue_id" in txt  # FileStore đặt id thật
    assert "test_command" in txt
    assert "blocked" in txt
    assert len(txt) < 3000
    ev = render_contract(reg.schema("evidence"), "evidence.json")
    assert "if verdict = fail" in ev
    plan = render_contract(reg.schema("plan"), "plan.yaml")
    assert (
        "· *reason: string ≤200 chars" in plan and "· *path: string" not in plan
    )  # 1 cấp: files là array<object>
    dec = render_contract(reg.schema("decision"), "decision.json")
    assert "· regression: string ∈ pass | fail | skipped" in dec and "Write `decision.json`" in dec

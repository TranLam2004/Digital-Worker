from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from digital_development_team.core.context import Actor, BudgetRef, CancelToken
from digital_development_team.core.enums import IssueStatus, Stage, Tier
from digital_development_team.core.errors import Cancelled
from digital_development_team.core.ids import next_issue_id
from digital_development_team.core.models import BudgetUsage, Checkpoint, Issue, ToolCall, ToolResult

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def test_tier_rank_bump_max() -> None:
    assert Tier.T1.rank == 1
    assert Tier.T2.bump() == Tier.T3
    assert Tier.T3.bump() == Tier.T3
    assert Tier.max_of(Tier.T0, None, Tier.T2, Tier.T1) == Tier.T2
    assert Tier.max_of() == Tier.T1


def test_stage_terminal_and_wait() -> None:
    assert Stage.DONE.terminal and Stage.BLOCKED.terminal
    assert not Stage.IMPLEMENT.terminal
    assert Stage.G1_WAIT.is_wait and not Stage.GATE.is_wait


def test_issue_id_pattern() -> None:
    Issue(id="DDT-0001", source="csv", source_id="1", title="t", body="b", received_at=NOW)
    with pytest.raises(ValidationError):
        Issue(id="ISSUE-1", source="csv", source_id="1", title="t", body="b", received_at=NOW)
    assert next_issue_id(None) == "DDT-0001"
    assert next_issue_id("DDT-0041") == "DDT-0042"


def test_tool_result_error_iff_not_ok() -> None:
    ToolResult.ok("fine", n=1)
    ToolResult.fail("TIMEOUT", "quá lâu", hint="chia nhỏ")
    with pytest.raises(ValidationError):
        ToolResult(status="ok", summary="x", error={"code": "X", "message": "m"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ToolResult(status="error", summary="x")


def test_tool_result_denied_shape() -> None:
    r = ToolResult.denied("scope_violation", "ngoài allowlist", hint="user.ask", allowed=["a.py"])
    assert r.status == "denied"
    assert r.structured["rule"] == "scope_violation"
    assert r.error is not None and r.error.code == "POLICY_DENIED"
    assert '"status": "denied"' in r.to_model_text()


def test_tool_call_args_hash_stable() -> None:
    a = ToolCall(
        id="t1",
        name="fs.read",
        args={"path": "a.py", "lines": "1-5"},
        actor=Actor(role="dev"),
        issue_id="DDT-0001",
        stage=Stage.IMPLEMENT,
    )
    b = ToolCall(
        id="t2",
        name="fs.read",
        args={"lines": "1-5", "path": "a.py"},
        actor=Actor(role="dev"),
        issue_id="DDT-0001",
        stage=Stage.IMPLEMENT,
    )
    assert a.args_hash() == b.args_hash()


def test_budget_usage_add() -> None:
    u = BudgetUsage(turns=1, tokens_in=10, tokens_out=5) + BudgetUsage(turns=2, tokens_in=1, cost_usd=0.5)
    assert u.turns == 3 and u.tokens == 16 and u.cost_usd == 0.5


def test_checkpoint_roundtrip() -> None:
    cp = Checkpoint(
        issue_id="DDT-0001",
        stage=Stage.IMPLEMENT,
        phase="stage_done",
        status=IssueStatus.RUNNING,
        saved_at=NOW,
    )
    again = Checkpoint.model_validate_json(cp.model_dump_json())
    assert again == cp
    assert not cp.terminal


def test_actor_str() -> None:
    assert str(Actor(role="dev")) == "dev"
    assert str(Actor(role="dev", sub_id="sub-1")) == "dev/sub-1"
    assert Actor(role="human:P1").is_human


def test_budget_ref_validation() -> None:
    BudgetRef(turns=1, wall_s=1.0, tokens=1)
    with pytest.raises(ValidationError):
        BudgetRef(turns=0, wall_s=1.0, tokens=1)


def test_cancel_token_parent_and_deadline() -> None:
    parent = CancelToken()
    child = parent.child(timeout_s=10, mono_now=0.0)
    assert not child.is_cancelled(mono_now=5.0)
    assert child.is_cancelled(mono_now=11.0)
    assert child.reason == "deadline"
    other = parent.child()
    parent.cancel("stop")
    assert other.is_cancelled()
    with pytest.raises(Cancelled):
        other.raise_if_cancelled()

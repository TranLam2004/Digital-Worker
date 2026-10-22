"""gates.decisions — logic quyết gate dùng chung CLI/web: lựa chọn hợp lệ, lý do bắt buộc, batch T0."""

from __future__ import annotations

from pathlib import Path

import pytest

from digital_development_team.core.context import FakeClock
from digital_development_team.core.enums import GateId, Tier
from digital_development_team.core.models import GateRequest
from digital_development_team.gates import (
    GateError,
    GateService,
    batch_approve,
    check_choice,
    pick_choice,
    resolve_gate,
)
from digital_development_team.storage import Database


def test_pick_choice_exactly_one() -> None:
    assert pick_choice(approve=True) == "approve"
    assert pick_choice(choice="A") == "A"
    with pytest.raises(GateError):
        pick_choice()
    with pytest.raises(GateError):
        pick_choice(approve=True, reject=True)


def test_check_choice_per_gate_and_note() -> None:
    check_choice(GateId.G1, "approve", None)
    check_choice(GateId.G1, "reject", "sai AC2")
    with pytest.raises(GateError, match="không có câu hỏi escalation"):
        check_choice(GateId.G1, "A", None)  # ticket không có form → không chọn chữ cái
    check_choice(GateId.G1, "B", None, options=["A", "B"])  # Leader/Dev hỏi ở G1
    with pytest.raises(GateError, match="không nhận"):
        check_choice(GateId.G2, "A", None, options=["A", "B"])
    with pytest.raises(GateError, match="cần lý do"):
        check_choice(GateId.G1, "reject", "   ")
    with pytest.raises(GateError, match="cần lý do"):
        check_choice(GateId.G1, "reject_final", None)
    check_choice(GateId.G0, "A", None, options=["A", "B"])
    with pytest.raises(GateError, match="không có option"):
        check_choice(GateId.G0, "C", None, options=["A", "B"])
    with pytest.raises(GateError, match="cần lý do"):
        check_choice(GateId.G0, "other", None, options=["A", "B"])
    with pytest.raises(GateError):
        check_choice(GateId.G3, "approve", None)


def _req(issue: str, gate: GateId, tier: Tier, attempt: int = 1) -> GateRequest:
    return GateRequest(issue_id=issue, gate=gate, attempt=attempt, tier=tier, summary={}, timeout_s=None)


def test_resolve_gate_writes_decision_and_keeps_open_on_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    svc = GateService(Database(tmp_path / "x.db"), fake_clock)
    t = svc.open(_req("DDT-0001", GateId.G1, Tier.T2))
    og = svc.get(t.id)
    assert og is not None
    with pytest.raises(GateError):
        resolve_gate(svc, og, choice="reject", by="human:P1", note=None, clock=fake_clock)
    assert svc.get(t.id).status == "open"  # type: ignore[union-attr]
    d = resolve_gate(svc, og, choice="reject", by="human:P1", note="thiếu test", clock=fake_clock)
    assert d.choice == "reject" and d.note == "thiếu test"
    polled = svc.poll(t)
    assert polled is not None and polled.by == "human:P1" and not polled.is_timeout


def test_batch_approve_only_allowed_tiers(tmp_path: Path, fake_clock: FakeClock) -> None:
    svc = GateService(Database(tmp_path / "x.db"), fake_clock)
    t0 = svc.open(_req("DDT-0001", GateId.G1, Tier.T0))
    t1 = svc.open(_req("DDT-0002", GateId.G1, Tier.T1))
    g0 = svc.open(_req("DDT-0003", GateId.G0, Tier.T0))
    ogs = [svc.get(t.id) for t in (t0, t1, g0)]
    done, skipped = batch_approve(svc, [o for o in ogs if o], by="human:P1", clock=fake_clock, tiers=["T0"])
    assert [d.ticket_id for d in done] == [t0.id, g0.id]
    assert skipped == [(t1.id, "tier T1 không trong batch_approve_tiers")]
    assert svc.get(t1.id).status == "open"  # type: ignore[union-attr]
    # ticket đã resolved → bỏ qua, không lỗi
    done2, skipped2 = batch_approve(svc, [o for o in ogs if o], by="human:P1", clock=fake_clock, tiers=["T0"])
    assert done2 == [] and len(skipped2) == 3

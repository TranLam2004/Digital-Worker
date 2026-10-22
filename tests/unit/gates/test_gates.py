from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from digital_development_team.core.context import FakeClock
from digital_development_team.core.enums import GateId, Tier
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import GateDecision, GateRequest
from digital_development_team.gates import GateService, StopSwitch
from digital_development_team.storage import Database, HomeLayout


def test_stop_switch(tmp_home: Path) -> None:
    sw = StopSwitch(HomeLayout(tmp_home).ensure())
    assert not sw.is_stopped() and sw.info() is None
    sw.request(by="P2")
    assert sw.is_stopped() and (sw.info() or "").startswith("P2 ")
    assert sw.clear() and not sw.is_stopped() and not sw.clear()


def _req(timeout: float | None = 90, attempt: int = 1) -> GateRequest:
    return GateRequest(
        issue_id="DDT-0001",
        gate=GateId.G0,
        attempt=attempt,
        tier=Tier.T1,
        summary={"type": "bug"},
        timeout_s=timeout,
    )


def test_gate_open_resolve_poll(tmp_path: Path, fake_clock: FakeClock) -> None:
    svc = GateService(Database(tmp_path / "x.db"), fake_clock)
    t = svc.open(_req())
    assert t.id == "DDT-0001:G0:1" and svc.open(_req()).id == t.id  # idempotent
    assert svc.poll(t) is None
    assert [g.id for g in svc.list_open()] == [t.id] and svc.open_for("DDT-0001", GateId.G0) is not None
    svc.resolve(t.id, GateDecision(choice="approve", by="human:P2", at=fake_clock.now(), note="ok"))
    d = svc.poll(t)
    assert d is not None and d.choice == "approve" and d.by == "human:P2" and not d.is_timeout
    assert svc.list_open() == []
    with pytest.raises(StoreError, match="đã resolved"):
        svc.resolve(t.id, GateDecision(choice="reject", by="x", at=fake_clock.now()))
    with pytest.raises(StoreError, match="không tồn tại"):
        svc.resolve("nope", GateDecision(choice="reject", by="x", at=fake_clock.now()))


def test_gate_timeout(tmp_path: Path, fake_clock: FakeClock) -> None:
    svc = GateService(Database(tmp_path / "x.db"), fake_clock)
    t = svc.open(_req(timeout=90, attempt=2))
    assert svc.poll(t, now=fake_clock.now() + timedelta(seconds=89)) is None
    d = svc.poll(t, now=fake_clock.now() + timedelta(seconds=90))
    assert d is not None and d.is_timeout and d.choice == "timeout"
    g = svc.get(t.id)
    assert g is not None and g.status == "timeout"
    # không timeout (T3)
    t3 = svc.open(_req(timeout=None, attempt=3))
    assert svc.poll(t3, now=fake_clock.now() + timedelta(days=1)) is None

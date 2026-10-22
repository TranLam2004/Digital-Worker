"""runner.worker — vòng lặp: chạy hết queue · park ở gate và chạy tiếp khi người quyết · STOP cắt stage đang chạy."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from digital_development_team.app import Runtime
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import GateId, IssueStatus, Stage
from digital_development_team.core.errors import ProviderPaused
from digital_development_team.core.models import GateDecision, StageResult
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService, StopSwitch
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.stub import stub_handlers
from digital_development_team.runner.state import IssueState
from digital_development_team.runner.worker import Worker
from digital_development_team.storage.heartbeat import Heartbeat, list_heartbeats
from tests.conftest import TESTDATA


def _registry(overrides: Mapping[str, Mapping[str, Any]] | None = None) -> StageRegistry:
    reg = default_handlers(stub=True)
    if overrides:
        reg = StageRegistry({**{s: reg.get(s) for s in reg.stages()}, **stub_handlers(overrides)})
    return reg


def _worker(rt: Runtime, reg: StageRegistry, *, poll_s: float = 0.01, watch_s: float = 0.05) -> Worker:
    svc = rt.services(with_agents=False)
    runner = Runner(svc, handlers=reg, stub=True)
    hb = Heartbeat(rt.db, rt.clock, worker_id="test:1", poll_s=poll_s)
    return Worker(runner, svc, heartbeat=hb, poll_s=poll_s, watch_s=watch_s)


def _ingest(rt: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )


def test_worker_drains_queue_until_idle(runtime: Runtime) -> None:
    _ingest(runtime)
    rep = _worker(runtime, _registry()).serve_forever(CancelToken(), until_idle=True)
    assert rep.exit_reason == "idle" and rep.runs == 3 and rep.statuses["done"] == 3
    assert {r.status for r in runtime.checkpoints.board()} == {IssueStatus.DONE}
    assert Queue(runtime.db, runtime.clock).counts() == {"done": 3}
    hbs = list_heartbeats(runtime.db)
    assert len(hbs) == 1 and hbs[0].state == "exited" and hbs[0].worker_id == "test:1"


def test_worker_parks_at_gate_and_resumes_after_resolve(runtime: Runtime, fake_clock: FakeClock) -> None:
    _ingest(runtime)
    reg = _registry({"intake": {"risk_tier": "T3"}})
    w = _worker(runtime, reg)
    rep = w.serve_forever(CancelToken(), until_idle=True)
    # T3 → park ở G0_WAIT. DDT-0002 blocked_by DDT-0001 nên queue chưa nhả → chỉ 0001 và 0003 chạy
    assert rep.runs == 2 and rep.statuses["waiting_gate"] == 2
    waiting = [r.issue_id for r in runtime.checkpoints.board() if r.status is IssueStatus.WAITING_GATE]
    assert waiting == ["DDT-0001", "DDT-0003"]
    assert w.candidates() == []  # ticket mở, T3 không timeout → chưa phải việc
    svc = GateService(runtime.db, fake_clock)
    og = svc.open_for("DDT-0001", GateId.G0)
    assert og is not None
    svc.resolve(og.id, GateDecision(choice="approve", by="human:P2", at=fake_clock.now(), note="ok"))
    assert w.candidates() == ["DDT-0001"]
    rep2 = w.serve_forever(CancelToken(), until_idle=True)
    assert rep2.runs == 1 and rep2.statuses["waiting_gate"] == 1  # → G1_WAIT (T3 bắt buộc người)
    g1 = svc.open_for("DDT-0001", GateId.G1)
    assert g1 is not None
    svc.resolve(g1.id, GateDecision(choice="approve", by="human:P1", at=fake_clock.now()))
    rep3 = w.serve_forever(CancelToken(), until_idle=True)
    assert rep3.statuses["done"] == 1
    cp = runtime.checkpoints.load("DDT-0001")
    assert cp is not None and cp.status is IssueStatus.DONE
    resolved = [e for e in read_events(runtime.layout, "DDT-0001") if e.event.value == "gate_resolved"]
    assert len(resolved) == 2 and all(e.human and e.human.wait_sec is not None for e in resolved)


class _SlowStage:
    """Giả lập IMPLEMENT chạy lâu: chỉ dừng khi token bị hủy (như agent loop kiểm cancel giữa tool call)."""

    stage = Stage.IMPLEMENT
    uses_llm = False

    def __init__(self) -> None:
        self.entered = threading.Event()

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        self.entered.set()
        while True:
            if ctx.cancel.wait(0.02):
                ctx.cancel.raise_if_cancelled()


def test_stop_interrupts_running_stage_within_watch_interval(runtime: Runtime) -> None:
    _ingest(runtime)
    slow = _SlowStage()
    reg = _registry().with_(IMPLEMENT=slow)
    w = _worker(runtime, reg, poll_s=0.02, watch_s=0.05)
    cancel = CancelToken()
    t = threading.Thread(target=w.serve_forever, args=(cancel,), daemon=True)
    t.start()
    assert slow.entered.wait(5.0), "IMPLEMENT chưa chạy"
    sw = StopSwitch(runtime.layout)
    t0 = time.monotonic()
    sw.request(by="P2")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        cp = runtime.checkpoints.load("DDT-0001")
        if cp is not None and cp.status is IssueStatus.PAUSED:
            break
        time.sleep(0.02)
    stopped_after = time.monotonic() - t0
    cancel.cancel("test done")
    t.join(timeout=5.0)
    cp = runtime.checkpoints.load("DDT-0001")
    assert cp is not None and cp.status is IssueStatus.PAUSED and cp.stage is Stage.IMPLEMENT
    assert stopped_after < 2.0, f"STOP mất {stopped_after:.2f}s"
    evs = read_events(runtime.layout, "DDT-0001")
    assert evs[-1].event.value == "stop_requested" and "STOP" in str(evs[-1].data.get("reason"))
    # resume: xoá STOP, IMPLEMENT bình thường → done
    sw.clear()
    rep = _worker(runtime, _registry()).serve_forever(CancelToken(), until_idle=True)
    assert rep.statuses["done"] >= 1
    assert runtime.checkpoints.load("DDT-0001").status is IssueStatus.DONE  # type: ignore[union-attr]


class _ProviderDownOnce:
    """IMPLEMENT của DDT-0001: lần đầu provider hết hạn mức (agent loop raise ProviderPaused), lần sau chạy như stub."""

    stage = Stage.IMPLEMENT
    uses_llm = False

    def __init__(self, inner: Any) -> None:
        self.calls: dict[str, int] = {}
        self._inner = inner

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        n = self.calls[state.issue_id] = self.calls.get(state.issue_id, 0) + 1
        if state.issue_id == "DDT-0001" and n == 1:
            raise ProviderPaused("claude_cli hết hạn mức subscription, không có fallback API")
        return self._inner.run(state, ctx)  # type: ignore[no-any-return]


def test_provider_outage_pauses_issue_and_worker_resumes_after_backoff(
    runtime: Runtime, fake_clock: FakeClock
) -> None:
    _ingest(runtime)
    base = _registry()
    down = _ProviderDownOnce(base.get(Stage.IMPLEMENT))
    svc = runtime.services(with_agents=False)
    runner = Runner(svc, handlers=base.with_(IMPLEMENT=down), stub=True)
    w = Worker(runner, svc, poll_s=0.01, provider_backoff_s=60)
    rep = w.serve_forever(CancelToken(), until_idle=True)
    # 0001 paused (không BLOCKED) · 0002 chờ 0001 · 0003 chạy bình thường
    assert rep.statuses["paused"] == 1 and rep.statuses["done"] == 1 and rep.statuses["blocked"] == 0
    cp = runtime.checkpoints.load("DDT-0001")
    assert cp is not None and cp.status is IssueStatus.PAUSED and cp.stage is Stage.IMPLEMENT
    last = read_events(runtime.layout, "DDT-0001")[-1]
    assert last.event.value == "stop_requested" and str(last.data["reason"]).startswith("PROVIDER_PAUSED")
    assert w.candidates() == []  # chưa hết backoff → không gọi lại provider
    fake_clock.advance(61)
    assert w.candidates()[0] == "DDT-0001"
    rep2 = w.serve_forever(CancelToken(), until_idle=True)
    assert rep2.statuses["done"] == 2 and down.calls["DDT-0001"] == 2
    assert runtime.checkpoints.load("DDT-0001").status is IssueStatus.DONE  # type: ignore[union-attr]

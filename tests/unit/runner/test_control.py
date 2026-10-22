"""Hàng lệnh người → worker: nạp backlog, ưu tiên, huỷ, chạy lại, backfill, dừng worker, merge sai; budget phiên."""

from __future__ import annotations

from dataclasses import replace

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, GateId, IssueStatus
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates import GateService
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.stub import stub_handlers
from digital_development_team.runner.worker import Worker
from digital_development_team.storage.control import ControlQueue
from digital_development_team.storage.layout import GLOBAL_ISSUE
from tests.conftest import TESTDATA


def _worker(rt: Runtime, overrides: dict | None = None) -> Worker:  # type: ignore[type-arg]
    reg = default_handlers(stub=True)
    if overrides:
        reg = StageRegistry({**{s: reg.get(s) for s in reg.stages()}, **stub_handlers(overrides)})
    svc = rt.services(with_agents=False)
    return Worker(Runner(svc, handlers=reg, stub=True), svc, poll_s=0.01, watch_s=0.05)


def _ingest(rt: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )


def _req(rt: Runtime, kind: str, issue: str | None = None, **payload: object) -> int:
    return ControlQueue(rt.db).submit(kind, by="human:P2", at=rt.clock.now(), issue=issue, payload=payload)


def _result(rt: Runtime, req_id: int) -> tuple[str, dict]:  # type: ignore[type-arg]
    r = next(x for x in ControlQueue(rt.db).recent() if x.id == req_id)
    return r.status, r.result or {}


def test_ingest_from_inbox_and_quick_issue(runtime: Runtime) -> None:
    inbox = runtime.layout.inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "b.csv").write_text(
        (TESTDATA / "backlogs" / "calc.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    a = _req(runtime, "ingest", path="inbox/b.csv")
    b = _req(
        runtime,
        "ingest",
        item={"title": "Thêm hàm mode() vào calc", "body": "mode([1,1,2]) == 1", "labels": ["enh"]},
    )
    w = _worker(runtime)
    w.tick(CancelToken())  # áp lệnh rồi chạy issue đầu
    st, res = _result(runtime, a)
    assert st == "done" and res["created"] == ["DDT-0001", "DDT-0002", "DDT-0003"]
    assert (inbox / "done" / "b.csv").is_file() and not (inbox / "b.csv").exists()
    st, res = _result(runtime, b)
    assert st == "done" and res["created"] == ["DDT-0004"]
    item = runtime.store.read("DDT-0004", "item.json").content
    assert item["source"] == "web" and item["raw"]["created_by"] == "human:P2"
    # file ngoài inbox bị từ chối
    bad = _req(runtime, "ingest", path="../evil.csv")
    w.tick(CancelToken())
    assert _result(runtime, bad)[0] == "failed"


def test_priority_cancel_and_retry(runtime: Runtime, fake_clock: FakeClock) -> None:
    _ingest(runtime)
    q = Queue(runtime.db, runtime.clock)
    p = _req(runtime, "set_priority", "DDT-0003", priority=1)
    c = _req(runtime, "cancel", "DDT-0002", note="trùng với issue khác")
    w = _worker(runtime, {"intake": {"risk_tier": "T3"}})
    assert w.tick(CancelToken()).startswith("ran:DDT-0003")  # ưu tiên mới chạy trước
    assert _result(runtime, p)[0] == "done" and q.get("DDT-0003").priority == 1  # type: ignore[union-attr]
    assert _result(runtime, c)[0] == "done" and q.get("DDT-0002").status == "cancelled"  # type: ignore[union-attr]
    # DDT-0003 đang chờ G0 (T3) → huỷ: BLOCKED, gate đóng, event có lý do
    cp = runtime.checkpoints.load("DDT-0003")
    assert cp is not None and cp.status is IssueStatus.WAITING_GATE
    c2 = _req(runtime, "cancel", "DDT-0003", note="người quyết bỏ")
    w.tick(CancelToken())
    assert _result(runtime, c2)[0] == "done"
    cp = runtime.checkpoints.load("DDT-0003")
    assert cp is not None and cp.status is IssueStatus.BLOCKED and cp.facts["cancelled"]["by"] == "human:P2"
    assert GateService(runtime.db, fake_clock).open_for("DDT-0003", GateId.G0) is None
    blocked = [e for e in read_events(runtime.layout, "DDT-0003") if e.event is EventType.BLOCKED]
    assert (
        blocked and blocked[-1].data["cancelled"] is True and "người quyết bỏ" in blocked[-1].data["reason"]
    )
    # retry chỉ cho issue đang tạm dừng
    r = _req(runtime, "retry", "DDT-0003")
    w.tick(CancelToken())
    assert _result(runtime, r)[0] == "failed"


def test_retry_clears_skip_and_worker_stop_exits(runtime: Runtime) -> None:
    _ingest(runtime)
    w = _worker(runtime)
    w._skip.add("DDT-0001")  # giả lập: worker đã bỏ qua sau 3 lần pause

    cp_runner = Runner(runtime.services(with_agents=False), handlers=default_handlers(stub=True), stub=True)
    stop = CancelToken()
    stop.cancel("STOP (human:P2)")
    cp_runner.run("DDT-0001", cancel=stop)  # → PAUSED
    assert runtime.checkpoints.load("DDT-0001").status is IssueStatus.PAUSED  # type: ignore[union-attr]
    assert "DDT-0001" not in w.candidates()
    r = _req(runtime, "retry", "DDT-0001")
    w._controls.apply_all()
    assert _result(runtime, r)[0] == "done" and "DDT-0001" in w.candidates()
    _req(runtime, "worker_stop")
    rep = w.serve_forever(CancelToken(), max_loops=5)
    assert rep.exit_reason == "người yêu cầu dừng worker"


def test_backfill_and_bad_merge(runtime: Runtime) -> None:
    _ingest(runtime)
    w = _worker(runtime)
    w.tick(CancelToken())  # DDT-0001 done (stub, Leader merge, không qua người)
    with runtime.db.transaction() as c:
        c.execute("DELETE FROM role_outcomes")
    b = _req(runtime, "backfill")
    w._controls.apply_all()
    assert _result(runtime, b) == ("done", {"added": 1})
    m = _req(runtime, "mark_bad_merge", "DDT-0001", note="mean([]) vẫn lỗi trên production")
    w._controls.apply_all()
    assert _result(runtime, m)[0] == "done"
    row = runtime.db.one("SELECT * FROM merge_feedback WHERE issue_id='DDT-0001'")
    assert row is not None and row["auto_merged"] == 1
    ver = runtime.db.one(
        "SELECT passed, reason FROM role_outcomes WHERE issue_id='DDT-0001' AND role='verifier'"
    )
    assert ver is not None and ver["passed"] == 0 and "merge sai" in ver["reason"]
    assert any(e.event is EventType.HUMAN_OVERRIDE for e in read_events(runtime.layout, "DDT-0001"))


def test_session_budget_stops_new_issues(runtime: Runtime) -> None:
    _ingest(runtime)
    cfg = load_config(overrides=[("budgets", "session.tokens", 10000)])  # stub tốn ~10k/issue
    rt = replace(runtime, config=cfg)
    w = _worker(rt)
    assert w.tick(CancelToken()).startswith("ran:DDT-0001")
    w._session_checked = -1e9  # bỏ cache 10 giây
    assert w.tick(CancelToken()) == "idle"  # đã chạm stop_new_at: không nhận issue mới
    warns = [e for e in read_events(rt.layout, GLOBAL_ISSUE) if e.event is EventType.BUDGET_WARNING]
    assert warns and warns[-1].data["scope"] == "session" and warns[-1].data["level"] == "stop"

"""Worker áp lệnh người gửi từ web (storage/control.py), giữa hai issue. Worker là writer duy nhất của event/checkpoint.

ingest         nạp file backlog trong inbox/ hoặc một issue nhập tay
set_priority   đổi ưu tiên trong hàng đợi (thấp = trước)
cancel         huỷ issue: còn trong hàng đợi → cancelled; đang làm → BLOCKED có lý do, đóng gate đang mở
retry          chạy lại issue đang tạm dừng: bỏ khỏi danh sách bỏ qua, xoá mốc chờ provider
reopen         mở lại issue BLOCKED (runner/reopen.py): stage đích, ghi chú, thêm file vào allowlist, cấp thêm budget
backfill       ghi số liệu học hỏi cho issue cũ
worker_stop    worker thoát êm sau tick này
mark_bad_merge người đánh dấu issue đã merge là sai (số liệu "merge tự động sai", episode human_correction)
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from digital_development_team.core.context import Actor
from digital_development_team.core.enums import EventType, IssueStatus, Stage
from digital_development_team.core.errors import DDTError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import GateDecision
from digital_development_team.gates.service import GateService
from digital_development_team.ingest.base import RawItem
from digital_development_team.ingest.pipeline import ingest_file, ingest_items
from digital_development_team.ingest.queue import Queue
from digital_development_team.runner.context import Services
from digital_development_team.runner.state import IssueState
from digital_development_team.storage.control import ControlQueue, ControlRequest

log = logging.getLogger(__name__)
RUNNER = Actor(role="runner")


@dataclass
class ApplyResult:
    applied: int = 0
    failed: int = 0
    stop_worker: bool = False


class ControlApplier:
    def __init__(
        self,
        svc: Services,
        *,
        gates: GateService,
        queue: Queue,
        on_retry: Callable[[str], None] = lambda _issue: None,
    ) -> None:
        self._svc = svc
        self._gates = gates
        self._queue = queue
        self._control = ControlQueue(svc.db)
        self._on_retry = on_retry

    def apply_all(self) -> ApplyResult:
        res = ApplyResult()
        for req in self._control.pending():
            try:
                ok, result = self._apply(req)
            except DDTError as e:
                ok, result = False, {"error": e.message}
            except Exception as e:  # lệnh hỏng không được giết worker
                log.exception("lệnh %s #%s lỗi", req.kind, req.id)
                ok, result = False, {"error": str(e)[:300]}
            self._control.complete(req.id, ok=ok, result=result, at=self._svc.clock.now())
            res.applied += 1
            res.failed += 0 if ok else 1
            if ok and req.kind == "worker_stop":
                res.stop_worker = True
        return res

    def _apply(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        handler = getattr(self, f"_do_{req.kind}", None)
        if handler is None:
            return False, {"error": f"worker chưa hiểu lệnh {req.kind}"}
        out: tuple[bool, dict[str, Any]] = handler(req)
        return out

    # ───── từng lệnh ─────
    def _do_ingest(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        p = req.payload
        svc = self._svc
        if p.get("path"):
            path = (svc.layout.home / str(p["path"])).resolve()
            inbox = svc.layout.inbox_dir.resolve()
            if inbox not in path.parents or not path.is_file():
                return False, {"error": "file không nằm trong inbox/"}
            rep = ingest_file(
                path,
                store=svc.store,
                queue=self._queue,
                clock=svc.clock,
                source_name=p.get("source_name") or None,
            )
            done = svc.layout.inbox_dir / "done"
            done.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                shutil.move(str(path), str(done / path.name))
        else:
            item = dict(p.get("item") or {})
            title = str(item.get("title") or "").strip()
            if not title:
                return False, {"error": "thiếu tiêu đề"}
            raw = RawItem(
                source="web",
                source_id=f"web-{req.id}",
                title=title[:300],
                body=str(item.get("body") or ""),
                labels=tuple(str(x) for x in item.get("labels") or ()),
                priority_hint=str(item["priority_hint"]) if item.get("priority_hint") else None,
                raw={"created_by": req.requested_by},
            )
            rep = ingest_items([raw], store=svc.store, queue=self._queue, clock=svc.clock, source="web")
        return not rep.errors or bool(rep.created), {
            "created": rep.created,
            "skipped": rep.skipped,
            "errors": rep.errors[:20],
            "summary": rep.summary(),
        }

    def _do_set_priority(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        issue = req.issue_id or ""
        row = self._queue.get(issue)
        if row is None:
            return False, {"error": f"{issue} không có trong hàng đợi"}
        prio = int(req.payload.get("priority", row.priority))
        self._queue.set_priority(issue, max(0, min(99, prio)))
        return True, {"priority": max(0, min(99, prio)), "was": row.priority}

    def _do_cancel(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        issue = req.issue_id or ""
        svc = self._svc
        note = str(req.payload.get("note") or "").strip()
        reason = f"người huỷ ({req.requested_by})" + (f": {note}" if note else "")
        cp = svc.checkpoints.load(issue)
        row = self._queue.get(issue)
        if cp is None:
            if row is None:
                return False, {"error": f"không có issue {issue}"}
            if row.status != "queued":
                return False, {"error": f"{issue} đang {row.status}, không huỷ được"}
            self._queue.set_status(issue, "cancelled")
            return True, {"status": "cancelled"}
        if cp.status.terminal:
            return False, {"error": f"{issue} đã {cp.status.value}"}
        state = IssueState.from_checkpoint(cp)
        from_stage = state.stage
        for og in self._gates.list_open(issue):  # gate đang mở không còn ai chờ
            self._gates.resolve(
                og.id,
                GateDecision(choice="reject", by=req.requested_by, at=svc.clock.now(), note="issue bị huỷ"),
            )
        state.pending_gate = None
        state.stage, state.phase, state.status = Stage.BLOCKED, "ready", IssueStatus.BLOCKED
        state.set_parallel(())
        state.facts["cancelled"] = {"by": req.requested_by, "note": note}
        svc.checkpoints.save(state.to_checkpoint(saved_at=svc.clock.now()))
        ev = svc.sink.emit(
            EventDraft(
                issue_id=issue,
                actor="runner",
                event=EventType.BLOCKED,
                stage=Stage.BLOCKED,
                data={"reason": reason, "from": from_stage.value, "cancelled": True},
            )
        )
        with contextlib.suppress(DDTError):
            cp2 = svc.checkpoints.load(issue)
            if cp2 is not None:
                svc.checkpoints.save(cp2.model_copy(update={"last_seq": ev.seq}))
        if row is not None:
            self._queue.set_status(issue, "blocked")
        return True, {"status": "blocked", "from": from_stage.value}

    def _do_retry(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        issue = req.issue_id or ""
        cp = self._svc.checkpoints.load(issue)
        if cp is None or cp.status not in (IssueStatus.PAUSED, IssueStatus.RUNNING, IssueStatus.WAITING_GATE):
            status = cp.status.value if cp else "chưa chạy"
            return False, {"error": f"{issue} đang {status}, chỉ chạy lại được issue đang tạm dừng"}
        self._on_retry(issue)
        return True, {"status": cp.status.value, "note": "worker sẽ chạy lại ở tick kế"}

    def _do_reopen(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        from digital_development_team.runner.reopen import reopen_issue

        p = req.payload
        issue = req.issue_id or ""
        ok, res = reopen_issue(
            self._svc,
            self._svc.config.policy,
            issue,
            by=req.requested_by,
            stage=str(p["stage"]) if p.get("stage") else None,
            option=str(p["option"]) if p.get("option") else None,
            note=str(p.get("note") or "") or None,
            allow=[str(x) for x in p.get("allow") or []],
            budget_pct=int(p.get("budget_pct") or 0),
        )
        if ok:
            self._on_retry(issue)
        return ok, res

    def _do_backfill(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        from digital_development_team.memory.outcomes import backfill_outcomes

        svc = self._svc
        added = backfill_outcomes(db=svc.db, layout=svc.layout, store=svc.store, checkpoints=svc.checkpoints)
        return True, {"added": added}

    def _do_worker_stop(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        return True, {"note": "worker thoát sau tick này"}

    def _do_mark_bad_merge(self, req: ControlRequest) -> tuple[bool, dict[str, Any]]:
        from digital_development_team.memory.outcomes import mark_bad_merge

        issue = req.issue_id or ""
        cp = self._svc.checkpoints.load(issue)
        if cp is None or cp.status is not IssueStatus.DONE:
            return False, {"error": f"{issue} chưa merge"}
        note = str(req.payload.get("note") or "").strip()
        if not note:
            return False, {"error": "cần lý do"}
        from digital_development_team.eventlog.reader import read_events

        human_g1 = any(
            e.event is EventType.GATE_RESOLVED
            and e.human
            and e.human.gate == "G1"
            and str(e.human.who).startswith("human")
            for e in read_events(self._svc.layout, issue)
        )
        mark_bad_merge(
            self._svc.db,
            issue,
            by=req.requested_by,
            note=note,
            at=self._svc.clock.now(),
            auto_merged=not human_g1,
        )
        self._svc.sink.emit(
            EventDraft(
                issue_id=issue,
                actor="runner",
                event=EventType.HUMAN_OVERRIDE,
                data={"kind": "bad_merge", "note": note},
            )
        )
        return True, {"marked": True}


__all__ = ["ApplyResult", "ControlApplier"]

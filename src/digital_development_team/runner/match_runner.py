"""MatchRunner — chạy một trận từ Start Match tới FINISHED, không người (người chạm máy = −50 điểm).

Một luồng điều phối, mỗi gamemaster.poll_s giây:
  1. đọc trang match (0 token) — lỗi mạng: thử lại; snapshot quá cũ thì hủy hợp tác worker
  2. đồng bộ: item mới → issue · đề đổi (title/mô tả/điểm) chưa nộp → issue phiên bản mới, huỷ bản cũ ·
     CLOSED → huỷ · hạn dời (PAUSED) → cập nhật match.json
  3. FINISHED/CANCELLED → huỷ việc và chờ grade có giới hạn · PAUSED → hủy hợp tác, không làm/nộp mới
  4. xếp lịch (runner/schedule.py) → giao issue cho slot rảnh; 1 slot dành riêng PI
  5. nộp lại bài lỗi (cùng Idempotency-Key) · hỏi kết quả chấm
Mỗi slot là một thread chạy Runner.run (gate autonomous không chờ). Thao tác git trên repo chính nối tiếp qua REPO_LOCK.
"""

from __future__ import annotations

import contextlib
import logging
import queue as _queue
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import EventType, IssueStatus
from digital_development_team.core.errors import ConfigError, DDTError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.gamemaster.models import ItemStatus, MatchSnapshot, MatchStatus
from digital_development_team.gamemaster.port import GameMaster
from digital_development_team.ingest.gamemaster_source import SOURCE, fingerprint, item_id_of, to_raw_item
from digital_development_team.ingest.pipeline import ingest_items
from digital_development_team.ingest.queue import Queue
from digital_development_team.runner.context import Services
from digital_development_team.runner.match import (
    needs_submit_retry,
    poll_grade,
    read_match,
    require_active,
    submit,
    write_match,
)
from digital_development_team.runner.runner import Runner, RunReport
from digital_development_team.runner.schedule import Candidate, pick, plan
from digital_development_team.storage.layout import GLOBAL_ISSUE
from digital_development_team.storage.submissions import SubmissionLedger

log = logging.getLogger(__name__)
SUPERSEDED = "SUPERSEDED (đề đổi — làm lại theo đề mới)"
CLOSED = "CLOSED (hết hạn item)"
MATCH_OVER = "MATCH OVER"


@dataclass
class MatchReport:
    ticks: int = 0
    dispatched: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    events: Counter[str] = field(default_factory=Counter)
    exit_reason: str = ""


@dataclass
class _Known:
    issue: str
    version: int
    fp: str


class MatchRunner:
    def __init__(
        self,
        svc: Services,
        gm: GameMaster,
        make_runner: Callable[[], Runner],
        *,
        slots: int = 2,
        pi_slot: bool = True,
        poll_s: float = 4.0,
        grade_poll_s: float = 10.0,
    ) -> None:
        self._svc = svc
        self._gm = gm
        self._queue = Queue(svc.db, svc.clock)
        self._slots = max(1, slots)
        self._pi_slot = pi_slot
        self._poll = poll_s
        self._grade_poll = grade_poll_s
        self._margin = float(svc.config.policy.deadline_margin_s)
        self._runners: _queue.Queue[Runner] = _queue.Queue()
        for _ in range(self._slots + (1 if pi_slot else 0)):
            self._runners.put(make_runner())
        self._pool = ThreadPoolExecutor(max_workers=self._runners.qsize(), thread_name_prefix="ddt-slot")
        self._lock = threading.Lock()
        self._running: dict[
            str, tuple[Future[RunReport], CancelToken, bool]
        ] = {}  # issue → (future, token, ở slot PI)
        self._known: dict[str, _Known] = {}
        self._dropped: set[str] = set()  # issue bị huỷ (đề đổi / CLOSED) — không chạy lại
        self._brief: dict[str, Any] = {}
        self._offset = timedelta()
        self._last_grade = -1e9
        self._last_snapshot = svc.clock.monotonic()
        self._finished_at: float | None = None
        self._retry_at: dict[str, float] = {}
        self.report = MatchReport()
        self._load_known()

    # ───── trạng thái đã biết (chạy lại process giữa trận) ─────
    def _load_known(self) -> None:
        for row in self._queue.list():
            if row.source != SOURCE:
                continue
            try:
                raw = dict(self._svc.store.read(row.issue_id, "item.json").content.get("raw") or {})
            except StoreError:
                continue
            if raw.get("match_id") != self._gm.match_id:
                raise ConfigError("This home already contains another match; use a separate --home per match")
            item_id = item_id_of(row.source_id)
            version = int(raw.get("version", 1))
            cur = self._known.get(item_id)
            if cur is None or version > cur.version:
                self._known[item_id] = _Known(row.issue_id, version, str(raw.get("fingerprint", "")))
            if row.status == "superseded":
                self._dropped.add(row.issue_id)

    # ───── vòng lặp ─────
    def run(self, cancel: CancelToken, *, max_ticks: int | None = None) -> MatchReport:
        try:
            while not cancel.is_cancelled():
                if max_ticks is not None and self.report.ticks >= max_ticks:
                    self.report.exit_reason = "max_ticks"
                    break
                outcome = self.tick()
                if outcome == "over":
                    self.report.exit_reason = "match over"
                    break
                if cancel.wait(self._poll):
                    break
            else:
                self.report.exit_reason = cancel.reason or "cancelled"
        finally:
            self.shutdown()
        return self.report

    def tick(self) -> str:
        self.report.ticks += 1
        self._reap()
        try:
            snap = self._gm.snapshot()
        except DDTError as e:  # mạng chập chờn: việc đang chạy vẫn chạy, tick sau đọc lại
            log.warning("đọc trang match lỗi: %s", e.message)
            if (
                self._svc.clock.monotonic() - self._last_snapshot
                >= self._svc.config.policy.gamemaster.snapshot_max_age_s
            ):
                self._cancel_all("STALE MATCH STATE")
            if self._finished_at is not None:
                return self._finish_tick()
            return "gm_error"
        self._last_snapshot = self._svc.clock.monotonic()
        self._offset = self._svc.clock.now() - snap.server_time
        self.sync(snap)
        if snap.status.over or self._finished_at is not None:
            self._cancel_all(MATCH_OVER)
            if self._finished_at is None:
                self._finished_at = self._svc.clock.monotonic()
            return self._finish_tick()
        if snap.status is not MatchStatus.IN_PROGRESS:
            self._cancel_all(f"MATCH {snap.status.value}")
            self._retry_and_grade(allow_submit=False)
            return snap.status.value.lower()  # READY/PREPARING/PAUSED: không nhận việc mới
        self._retry_and_grade()
        self._dispatch()
        return "ok"

    def _finish_tick(self) -> str:
        self._retry_and_grade(allow_submit=False)
        pending = any(
            (read_match(self._svc, i).get("submission") or {}).get("grade") == "PENDING"
            for i in self._submission_issues()
        )
        with self._lock:
            pending = pending or bool(self._running)
        elapsed = self._svc.clock.monotonic() - (self._finished_at or 0.0)
        if pending and elapsed < self._svc.config.policy.gamemaster.final_grade_timeout_s:
            return "grading"
        if pending:
            self.report.events["grade_timeout"] += 1
        return "over"

    def shutdown(self, wait: bool = True) -> None:
        self._cancel_all("MATCH RUNNER STOPPED")
        self._pool.shutdown(wait=wait, cancel_futures=True)
        self._reap()

    # ───── đồng bộ trang match ─────
    def sync(self, snap: MatchSnapshot) -> None:
        if snap.brief and snap.brief != self._brief:
            self._brief = dict(snap.brief)
        for item in snap.items:
            fp = fingerprint(item)
            known = self._known.get(item.id)
            if known is None:
                if item.status is not ItemStatus.CLOSED:
                    self._ingest(item, snap, version=1, fp=fp)
                continue
            if fp != known.fp and known.issue not in self._dropped:
                if SubmissionLedger(self._svc.db).get(snap.match_id, item.id) or (
                    read_match(self._svc, known.issue).get("submission") or {}
                ).get("idempotency_key"):
                    known.fp = fp  # đã nộp: luật 1 lần/item, đề đổi không làm gì được nữa
                    self._event(known.issue, "changed_after_submit", item.id)
                else:
                    self._drop(known.issue, SUPERSEDED, "superseded")
                    self._ingest(item, snap, version=known.version + 1, fp=fp, supersedes=known.issue)
                    continue
            self._update(known.issue, item, snap)
            if item.status is ItemStatus.CLOSED and not self._terminal(known.issue):
                self._drop(known.issue, CLOSED, "blocked")
                self._event(known.issue, "closed", item.id)

    def _ingest(
        self, item: Any, snap: MatchSnapshot, *, version: int, fp: str, supersedes: str | None = None
    ) -> None:
        raw_item = to_raw_item(item, match_id=snap.match_id, offset=self._offset, version=version)
        if supersedes:
            raw_item = raw_item.model_copy(update={"raw": {**raw_item.raw, "supersedes": supersedes}})
        rep = ingest_items(
            [raw_item], store=self._svc.store, queue=self._queue, clock=self._svc.clock, source=SOURCE
        )
        if not rep.created:
            log.warning("ingest %s lỗi: %s", item.id, rep.errors)
            return
        issue = rep.created[0]
        self._known[item.id] = _Known(issue, version, fp)
        from digital_development_team.gamemaster.fake import FakeGameMaster

        folder = (
            str(snap.repository.get("folder") or ".")
            if isinstance(self._gm, FakeGameMaster)
            else self._svc.config.policy.gamemaster.match_folder.format(match_id=snap.match_id)
        )
        write_match(
            self._svc,
            issue,
            {
                "status": item.status.value,
                "application_brief": self._brief,
                "work_folder": folder.replace("\\", "/"),
            },
        )
        self._update(issue, item, snap)
        self._event(issue, "changed" if supersedes else "new", item.id, supersedes=supersedes)

    def _update(self, issue: str, item: Any, snap: MatchSnapshot) -> None:
        """Hạn dời (trận PAUSED) · trạng thái item → match.json (runner đọc lại hạn mỗi vòng)."""
        if self._terminal(issue):
            return
        cur = read_match(self._svc, issue)
        deadlines = [d for d in (item.deadline_at, snap.ends_at) if d is not None]
        local = (min(deadlines) + self._offset).isoformat() if deadlines else None
        upd: dict[str, Any] = {}
        if local and cur.get("deadline_at") != local:
            upd["deadline_at"] = local
        if cur.get("status") != item.status.value:
            upd["status"] = item.status.value
        if upd:
            write_match(self._svc, issue, upd)

    def _terminal(self, issue: str) -> bool:
        cp = self._svc.checkpoints.load(issue)
        return cp is not None and cp.status.terminal

    def _drop(self, issue: str, reason: str, status: str) -> None:
        self._dropped.add(issue)
        with self._lock:
            entry = self._running.get(issue)
        if entry is not None:
            entry[1].cancel(reason)
        q = self._queue.get(issue)
        if q is not None and q.status not in ("done",):
            self._queue.set_status(issue, status)

    # ───── giao việc ─────
    def _types(self) -> dict[str, tuple[str, float | None]]:
        out: dict[str, tuple[str, float | None]] = {}
        for item_id, k in self._known.items():
            m = read_match(self._svc, k.issue)
            out[k.issue] = (str(m.get("type") or item_id.split("-", 1)[0]), m.get("points"))
        return out

    def _candidates(self) -> list[Candidate]:
        types = self._types()
        out: list[Candidate] = []
        with self._lock:
            busy = set(self._running)
        for row in self._queue.list():
            if row.source != SOURCE or row.issue_id in busy or row.issue_id in self._dropped:
                continue
            if row.issue_id not in types or self._retry_at.get(row.issue_id, 0) > self._svc.clock.monotonic():
                continue
            resumable = row.status == "running" and self._resumable(row.issue_id)
            if row.status != "queued" and not resumable:
                continue
            t, pts = types.get(row.issue_id, ("TASK", None))
            m = read_match(self._svc, row.issue_id)
            dl = m.get("deadline_at")
            out.append(
                Candidate(
                    row.issue_id,
                    t,
                    pts,
                    datetime.fromisoformat(str(dl)) if dl else None,
                    row.enqueued_at,
                )
            )
        return out

    def _resumable(self, issue: str) -> bool:
        """Issue dở dang (process trước chết / provider tạm dừng) → chạy tiếp từ checkpoint."""
        cp = self._svc.checkpoints.load(issue)
        return cp is not None and cp.status in (IssueStatus.PAUSED, IssueStatus.RUNNING)

    def _dispatch(self) -> None:
        from digital_development_team.budget.session import session_status, session_totals

        usages = [cp.usage for row in self._queue.list() if (cp := self._svc.checkpoints.load(row.issue_id))]
        tokens, usd = session_totals(usages)
        if session_status(tokens, usd, self._svc.config.budgets.session)["level"] == "stop":
            return
        cands = self._candidates()
        if not cands:
            return
        p = plan(cands, now=self._svc.clock.now(), margin_s=self._margin)
        for issue, why in p.skip:
            self._drop(issue, why, "blocked")
            self._event(issue, "skipped", issue, reason=why)
        with self._lock:
            used_pi = sum(1 for _, _, on_pi in self._running.values() if on_pi)
            used = len(self._running) - used_pi
        free_pi = (1 if self._pi_slot else 0) - used_pi
        free_general = self._slots - used
        types = {c.issue_id: c.item_type for c in cands}
        for issue in pick(p.order, types, free_general=max(free_general, 0), free_pi=max(free_pi, 0)):
            on_pi = types.get(issue) == "PI" and free_pi > 0
            if on_pi:
                free_pi -= 1
            self._start(issue, on_pi=on_pi)

    def _start(self, issue: str, *, on_pi: bool) -> None:
        token = CancelToken()
        self._queue.set_status(
            issue, "running"
        )  # nhận việc ngay trong luồng điều phối — không slot nào lấy trùng
        fut = self._pool.submit(self._run_slot, issue, token)
        with self._lock:
            self._running[issue] = (fut, token, on_pi)
        self.report.dispatched += 1

    def _run_slot(self, issue: str, token: CancelToken) -> RunReport:
        runner = self._runners.get()
        try:
            return runner.run(issue, cancel=token, wait_gates=True, poll_s=0.5)
        finally:
            self._runners.put(runner)

    def _reap(self) -> None:
        with self._lock:
            done = [(i, f) for i, (f, _, _) in self._running.items() if f.done()]
            for i, _ in done:
                del self._running[i]
        for issue, fut in done:
            try:
                rep = fut.result()
                self.report.statuses[rep.status] += 1
                if rep.status == "paused":
                    self._retry_at[issue] = (
                        self._svc.clock.monotonic() + self._svc.config.policy.gamemaster.retry_backoff_s
                    )
            except Exception as e:  # lỗi một issue không được giết cả trận
                log.error("%s: slot lỗi: %s", issue, e)
                self.report.statuses["error"] += 1

    def _cancel_all(self, reason: str) -> None:
        with self._lock:
            tokens = [t for _, t, _ in self._running.values()]
        for t in tokens:
            t.cancel(reason)

    # ───── nộp lại · kết quả chấm ─────
    def _submission_issues(self) -> set[str]:
        return {k.issue for k in self._known.values()} | set(
            SubmissionLedger(self._svc.db).issues(self._gm.match_id)
        )

    def _retry_and_grade(self, *, allow_submit: bool = True) -> None:
        now = self._svc.clock.monotonic()
        grade_due = now - self._last_grade >= self._grade_poll
        if grade_due:
            self._last_grade = now
        for issue in self._submission_issues():
            if grade_due:
                poll_grade(self._svc, issue)
            if not allow_submit or issue in self._dropped or not self._terminal(issue):
                continue
            if self._retry_at.get(issue, 0) > now:
                continue
            self._retry_at[issue] = now + self._svc.config.policy.gamemaster.retry_backoff_s
            if needs_submit_retry(self._svc, issue):
                sha = str((read_match(self._svc, issue).get("submission") or {}).get("git_revision"))
                with contextlib.suppress(DDTError):
                    submit(self._svc, issue, sha)
            elif self._unpushed(issue):
                self._push_and_submit(issue)

    def _unpushed(self, issue: str) -> str | None:
        """DONE nhưng push trong MERGE lỗi (chưa nộp) → sha cần push lại."""
        cp = self._svc.checkpoints.load(issue)
        m = read_match(self._svc, issue)
        if cp is None or not cp.status.terminal or m.get("submission"):
            return None
        raw_merge = cp.facts.get("merge")
        sha = raw_merge.get("sha") if isinstance(raw_merge, dict) else None
        verified = m.get("verification") or {}
        if verified.get("status") == "pass":
            sha = verified.get("git_revision") or sha
        return str(sha) if sha else None

    def _push_and_submit(self, issue: str) -> None:
        sha = self._unpushed(issue)
        wt = self._svc.worktrees
        remote = self._svc.config.policy.gamemaster.push_remote
        if sha is None or wt is None or not remote:
            return
        from digital_development_team.isolation.worktree import REPO_LOCK

        try:
            with REPO_LOCK:
                require_active(self._svc, issue)
                wt.git.push_branch(remote, wt.default_branch())
                wt.git.verify_pushed(remote, wt.default_branch(), sha)
        except DDTError as e:
            log.warning("%s: push lại lỗi: %s", issue, e.message)
            return
        with contextlib.suppress(DDTError):
            submit(self._svc, issue, sha)

    def _event(self, issue: str, kind: str, item_id: str, **data: Any) -> None:
        self.report.events[kind] += 1
        self._svc.sink.emit(
            EventDraft(
                issue_id=issue if issue.startswith("DDT-") else GLOBAL_ISSUE,
                actor="runner",
                event=EventType.MATCH_ITEM,
                data={"kind": kind, "item": item_id, **{k: v for k, v in data.items() if v is not None}},
            )
        )

"""`ddt worker` — vòng lặp sống lâu bọc Runner (10-harness/01-runner · 70-human-control/02-gates G3).

Mỗi tick:
  STOP?  → heartbeat 'stopped', không nhận việc, chờ
  chọn issue: (1) đang chờ gate mà người đã quyết / hết timeout  (2) paused · running mồ côi  (3) queue.next()
  Runner.run(issue, wait_gates=False) → chờ gate thì park, DONE/BLOCKED thì tick tiếp.

Rảnh (không issue nào chờ) và learn=True → chạy một job Learner (runner/learn.py), STOP vẫn dừng được nó.

Watcher (daemon thread, 0.5s): thấy state/STOP → cancel token của issue đang chạy → stage dừng ở tool call kế
(STOP < 5s thay vì hết stage) · beat heartbeat kèm issue/stage hiện tại.

Worker là process DUY NHẤT chạy pipeline. Lệnh CLI khác chỉ ghi control_requests/gate_requests/STOP; worker nhặt ở tick kế.
v1 chạy tuần tự (1 issue một lúc). WIP song song: P2#21 (lock ChangeIntent) — tham số `wip` giữ chỗ.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from digital_development_team.budget.session import LEVELS, session_status, session_totals
from digital_development_team.config.loader import ResolvedConfig
from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import EventType, IssueStatus
from digital_development_team.core.errors import DDTError
from digital_development_team.core.events import EventDraft
from digital_development_team.gates.service import GateService
from digital_development_team.gates.stop import StopSwitch
from digital_development_team.ingest.queue import Queue
from digital_development_team.runner.context import Services
from digital_development_team.runner.control import ControlApplier
from digital_development_team.runner.runner import Runner, RunReport
from digital_development_team.storage.control import ControlQueue
from digital_development_team.storage.heartbeat import Heartbeat
from digital_development_team.storage.layout import GLOBAL_ISSUE

log = logging.getLogger(__name__)

STOP_REASON = "STOP (worker watcher)"
CANCEL_REASON = "CANCEL (người huỷ issue)"
SESSION_CHECK_S = 10.0


@dataclass
class WorkerReport:
    loops: int = 0
    runs: int = 0
    learned: int = 0
    idle_ticks: int = 0
    stopped_ticks: int = 0
    last: RunReport | None = None
    statuses: Counter[str] = field(default_factory=Counter)
    exit_reason: str = ""


class _Shared:
    """Trạng thái chia sẻ giữa vòng lặp chính và watcher thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.issue: str | None = None
        self.cancel: CancelToken | None = None
        self.state = "idle"
        self.loop_n = 0

    def set_running(self, issue: str, cancel: CancelToken) -> None:
        with self.lock:
            self.issue, self.cancel, self.state = issue, cancel, "running"

    def set_idle(self, state: str = "idle") -> None:
        with self.lock:
            self.issue, self.cancel, self.state = None, None, state

    def snapshot(self) -> tuple[str, str | None, CancelToken | None, int]:
        with self.lock:
            return self.state, self.issue, self.cancel, self.loop_n


class Watcher(threading.Thread):
    """Daemon: poll STOP mỗi poll_s → cancel issue đang chạy; beat heartbeat. Không bao giờ raise."""

    def __init__(
        self,
        *,
        stop: StopSwitch,
        shared: _Shared,
        heartbeat: Heartbeat | None,
        svc: Services,
        poll_s: float = 0.5,
    ) -> None:
        super().__init__(name="ddt-worker-watcher", daemon=True)
        self._stop = stop
        self._shared = shared
        self._hb = heartbeat
        self._svc = svc
        self._poll = poll_s
        self._done = threading.Event()

    def close(self) -> None:
        self._done.set()

    def _stage_of(self, issue: str | None) -> str | None:
        if issue is None:
            return None
        try:
            cp = self._svc.checkpoints.load(issue)
        except DDTError:
            return None
        return cp.stage.value if cp else None

    def run(self) -> None:  # pragma: no cover - thread body kiểm qua test tích hợp
        while not self._done.wait(self._poll):
            try:
                state, issue, cancel, loop_n = self._shared.snapshot()
                if cancel is not None and self._stop.is_stopped() and not cancel.is_cancelled():
                    cancel.cancel(STOP_REASON)
                    log.info("STOP thấy khi %s đang chạy → cancel", issue)
                if (
                    cancel is not None
                    and issue
                    and not cancel.is_cancelled()
                    and ControlQueue(self._svc.db).pending(kind="cancel", issue=issue)
                ):
                    cancel.cancel(CANCEL_REASON)  # dừng ở tool call kế; tick sau áp lệnh huỷ → BLOCKED
                    log.info("người huỷ %s khi đang chạy", issue)
                if self._hb is not None:
                    self._hb.beat(
                        state, current_issue=issue, current_stage=self._stage_of(issue), loop_n=loop_n
                    )
            except Exception as e:  # watcher là phụ trợ, không được giết worker
                log.warning("watcher lỗi: %s", e)


class Worker:
    def __init__(
        self,
        runner: Runner,
        svc: Services,
        *,
        stop: StopSwitch | None = None,
        gates: GateService | None = None,
        heartbeat: Heartbeat | None = None,
        poll_s: float = 2.0,
        watch_s: float = 0.5,
        wip: int | None = None,
        resume_stale: bool = True,
        max_pause_retries: int = 3,
        provider_backoff_s: float = 300.0,
        config_source: Callable[[], ResolvedConfig] | None = None,
        assets_fingerprint: Callable[[], str] | None = None,
        rebuild: Callable[[ResolvedConfig | None], Runner] | None = None,
        learn: bool = False,
    ) -> None:
        self._runner = runner
        self._config_source = config_source
        self._assets_fp = assets_fingerprint
        self._rebuild = rebuild
        self._fingerprint: str | None = None
        self._assets_prev: str | None = None
        self._primed = False
        self._svc = svc
        self._stop = stop or StopSwitch(svc.layout)
        self._gates = gates or GateService(svc.db, svc.clock)
        self._hb = heartbeat
        self._poll = poll_s
        self._watch_s = watch_s
        self._wip = wip
        self._resume_stale = resume_stale
        self._max_pause = max_pause_retries
        self._queue = Queue(svc.db, svc.clock)
        self._shared = _Shared()
        self._pauses: Counter[str] = Counter()
        self._skip: set[str] = set()
        self._provider_backoff = provider_backoff_s
        self._not_before: dict[str, float] = {}  # issue paused do provider → không chạy lại trước mốc này
        self._controls = ControlApplier(svc, gates=self._gates, queue=self._queue, on_retry=self._retry)
        self._session_level = "ok"
        self._session_checked = -1e9
        self._learn = learn

    def _retry(self, issue: str) -> None:
        """Người bấm chạy lại: bỏ khỏi danh sách bỏ qua, xoá đếm pause và mốc chờ provider."""
        self._skip.discard(issue)
        self._pauses.pop(issue, None)
        self._not_before.pop(issue, None)

    def _session(self) -> str:
        """Mức budget phiên (budget/session.py), tính lại mỗi SESSION_CHECK_S. Qua mức mới → budget_warning toàn cục."""
        now = self._svc.clock.monotonic()
        if now - self._session_checked < SESSION_CHECK_S:
            return self._session_level
        self._session_checked = now
        cfg = self._svc.config
        if self._config_source is not None:
            with contextlib.suppress(DDTError):
                cfg = self._config_source()
        usages = []
        for r in self._svc.checkpoints.board():
            cp = self._svc.checkpoints.load(r.issue_id)
            if cp is not None:
                usages.append(cp.usage)
        st = session_status(*session_totals(usages), cfg.budgets.session)
        level = str(st["level"])
        if LEVELS.index(level) > LEVELS.index(self._session_level):
            self._svc.sink.emit(
                EventDraft(
                    issue_id=GLOBAL_ISSUE,
                    actor="runner",
                    event=EventType.BUDGET_WARNING,
                    data={"scope": "session", **st},
                )
            )
            log.warning("budget phiên %s (%.0f%%)", level, st["ratio"] * 100)
        self._session_level = level
        return level

    def _waiting_provider(self, issue: str) -> bool:
        t = self._not_before.get(issue)
        return t is not None and self._svc.clock.monotonic() < t

    # ───── chọn việc ─────
    def _gate_ready(self, issue: str, now: datetime) -> bool:
        cp = self._svc.checkpoints.load(issue)
        if cp is None or not cp.pending_gate:
            return True  # không có ticket → để runner xử lý
        og = self._gates.get(cp.pending_gate)
        if og is None or og.status != "open":
            return True
        t = og.ticket
        return t.timeout_s is not None and (now - t.opened_at).total_seconds() >= float(t.timeout_s)

    def candidates(self) -> list[str]:
        """Thứ tự: gate đã quyết → paused/running mồ côi → queue. Không lặp issue đã skip."""
        now = self._svc.clock.now()
        out: list[str] = []
        rows = self._svc.checkpoints.board()
        for r in rows:
            if r.issue_id in self._skip:
                continue
            if r.status is IssueStatus.WAITING_GATE and self._gate_ready(r.issue_id, now):
                out.append(r.issue_id)
        if self._resume_stale:
            for r in rows:
                if r.issue_id in self._skip or r.issue_id in out or self._waiting_provider(r.issue_id):
                    continue
                if r.status in (IssueStatus.PAUSED, IssueStatus.RUNNING):
                    out.append(r.issue_id)
        nxt = None if self._session() == "stop" else self._queue.next()  # hết budget phiên: chỉ làm nốt
        if (
            nxt is not None
            and nxt.issue_id not in out
            and nxt.issue_id not in self._skip
            and not self._waiting_provider(nxt.issue_id)
        ):
            out.append(nxt.issue_id)
        return out

    # ───── một tick ─────
    def tick(self, cancel: CancelToken) -> str:
        if self._controls.apply_all().stop_worker:
            return "exit"
        if self._stop.is_stopped():
            self._shared.set_idle("stopped")
            return "stopped"
        cands = self.candidates()
        if not cands:
            learned = self._learn_one(cancel)
            if learned is not None:
                return f"learned:{learned}"
            self._shared.set_idle("idle")
            return "idle"
        issue = cands[0]
        self._maybe_reload(issue)
        issue_cancel = cancel.child()
        self._shared.set_running(issue, issue_cancel)
        if self._hb is not None:
            self._hb.beat("running", current_issue=issue, loop_n=self._shared.loop_n)
        try:
            rep = self._runner.run(issue, cancel=issue_cancel, wait_gates=False)
        finally:
            self._shared.set_idle("idle")
        self._after(rep)
        return f"ran:{issue}:{rep.status}"

    def _learn_one(self, cancel: CancelToken) -> str | None:
        """Một job Learner khi rảnh. Hết budget phiên hoặc --no-learn → không học. Trả issue đã xử lý."""
        svc = self._runner.services
        if not self._learn or svc.config.policy.no_learn or self._session() == "stop":
            return None
        from digital_development_team.runner.learn import LearnRunner

        lr = LearnRunner(svc)
        issue = lr.next_job()
        if issue is None:
            return None
        job_cancel = cancel.child()
        self._shared.set_running(issue, job_cancel)
        try:
            res = lr.run_one(job_cancel, issue=issue)
        finally:
            self._shared.set_idle("idle")
        log.info("Learner %s: %s", issue, res[1] if res else "?")
        return issue

    @property
    def runner(self) -> Runner:
        return self._runner

    def _maybe_reload(self, issue: str) -> None:
        """Hot-reload giữa hai issue. Config (runtime.yaml · overrides/config) hoặc asset override (JD, skill, checklist,
        tool) đổi → dựng lại runner để issue sắp chạy dùng bản mới, emit config_reloaded vào issue đó.

        Config lỗi hoặc dựng lại lỗi → giữ runner cũ, ghi log; lần sửa kế (fingerprint đổi) sẽ thử lại.
        """
        cfg: ResolvedConfig | None = None
        if self._config_source is not None:
            try:
                cfg = self._config_source()
            except DDTError as e:
                log.error("config lỗi, giữ bản cũ: %s", e)
                return
        cfg_fp = cfg.fingerprint if cfg is not None else None
        assets_fp = self._assets_prev
        if self._assets_fp is not None:
            try:
                assets_fp = self._assets_fp()
            except OSError as e:
                log.warning("không đọc được lớp override: %s", e)
        changed: list[str] = []
        if self._primed:
            if cfg_fp != self._fingerprint:
                changed.append("config")
            if assets_fp != self._assets_prev:
                changed.append("assets")
        self._primed = True
        self._fingerprint, self._assets_prev = cfg_fp, assets_fp
        if not changed:
            return
        data: dict[str, object] = {"scope": "worker", "changed": changed}
        if cfg is not None:
            data["fingerprint"] = cfg.fingerprint[:12]
            data["patches"] = list(cfg.patches)[-5:]
        if assets_fp is not None:
            data["assets"] = assets_fp[:12]
        if self._rebuild is not None:
            try:
                self._runner = self._rebuild(cfg)
                data["rebuilt"] = True
            except DDTError as e:
                log.error("dựng lại runtime lỗi, giữ bản cũ: %s", e)
                data["rebuilt"] = False
                data["error"] = str(e)[:300]
        self._svc.sink.emit(
            EventDraft(issue_id=issue, actor="runner", event=EventType.CONFIG_RELOADED, data=data)
        )

    def _after(self, rep: RunReport) -> None:
        if rep.status == "paused":
            if rep.reason and "STOP" in rep.reason:
                return  # dừng do người — sẽ resume khi hết STOP
            if rep.reason and rep.reason.startswith("PROVIDER_PAUSED"):
                # lỗi provider không phải lỗi của issue: chờ backoff rồi thử lại, không đếm vào max_pause
                self._not_before[rep.issue_id] = self._svc.clock.monotonic() + self._provider_backoff
                log.warning("%s tạm dừng do provider — thử lại sau %ss", rep.issue_id, self._provider_backoff)
                return
            self._pauses[rep.issue_id] += 1
            if self._pauses[rep.issue_id] >= self._max_pause:
                log.warning(
                    "%s paused %d lần (%s) — bỏ qua, cần người xem", rep.issue_id, self._max_pause, rep.reason
                )
                self._skip.add(rep.issue_id)
        elif rep.status in ("done", "blocked"):
            self._pauses.pop(rep.issue_id, None)
            self._not_before.pop(rep.issue_id, None)

    # ───── vòng lặp ─────
    def serve_forever(
        self,
        cancel: CancelToken,
        *,
        max_loops: int | None = None,
        until_idle: bool = False,
    ) -> WorkerReport:
        """Chạy tới khi cancel · max_loops · (until_idle và không còn việc). Trả thống kê."""
        report = WorkerReport()
        watcher = Watcher(
            stop=self._stop, shared=self._shared, heartbeat=self._hb, svc=self._svc, poll_s=self._watch_s
        )
        watcher.start()
        try:
            while True:
                if cancel.is_cancelled():
                    report.exit_reason = cancel.reason or "cancelled"
                    break
                if max_loops is not None and report.loops >= max_loops:
                    report.exit_reason = "max_loops"
                    break
                report.loops += 1
                self._shared.loop_n = report.loops
                try:
                    outcome = self.tick(cancel)
                except DDTError as e:
                    log.error("tick lỗi: %s", e)
                    outcome = "error"
                if outcome == "exit":
                    report.exit_reason = "người yêu cầu dừng worker"
                    break
                if outcome.startswith("ran:"):
                    report.runs += 1
                    report.statuses[outcome.rsplit(":", 1)[-1]] += 1
                    continue  # còn việc → tick ngay
                if outcome.startswith("learned:"):
                    report.learned += 1
                    continue
                if outcome == "stopped":
                    report.stopped_ticks += 1
                else:
                    report.idle_ticks += 1
                    if until_idle:
                        report.exit_reason = "idle"
                        break
                if self._hb is not None:
                    self._hb.beat(outcome if outcome in ("idle", "stopped") else "idle", loop_n=report.loops)
                if cancel.wait(self._poll):
                    report.exit_reason = cancel.reason or "cancelled"
                    break
        finally:
            watcher.close()
            if self._hb is not None:
                with contextlib.suppress(DDTError):  # pragma: no cover
                    self._hb.exited()
        return report

    @property
    def wip(self) -> int | None:
        return self._wip

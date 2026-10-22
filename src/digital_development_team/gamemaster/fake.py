"""FakeGameMaster — Game Master giả lập để luyện tập không tốn trận thật (luật MTM v3.5).

Kịch bản JSON (testdata/matches/*.json):
  {"match_id": "M-PRACTICE-01", "duration_min": 40, "brief": {...},
   "items": [{"id": "BUG-01", "type": "BUG", "title": "...", "description": "...", "points": 200,
              "reveal_at_s": 0, "deadline_min": 20,
              "mvs": [{"name": "...", "steps": "...", "expected": "..."}],
              "submit_template": {"summary": "", "rootCause": "", "evidence": ""},
              "changes": [{"at_s": 300, "description": "đề mới"}],
              "grader": {"cmd": "python -m pytest -q tests/test_hidden.py",
                         "files": {"tests/test_hidden.py": "..."}}}]}

Luật mô phỏng: item chưa tới giờ reveal thì KHÔNG có trong snapshot · deadline tính từ reveal, không tính lúc PAUSED ·
MVS + template chỉ trả khi start working · submit khi chưa start → 409 · mỗi item nộp 1 lần (cùng Idempotency-Key +
body → trả bài cũ) · quá hạn → CLOSED, nộp → 409 · Grader chạy lệnh chấm (kèm test ẩn) trên đúng gitRevision.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from digital_development_team.core.context import CancelToken, Clock, SystemClock
from digital_development_team.gamemaster.http_client import GameMasterError
from digital_development_team.gamemaster.models import (
    BacklogItem,
    GradeResult,
    GradeStatus,
    ItemStatus,
    ItemType,
    MatchSnapshot,
    MatchStatus,
    StartResult,
    SubmitResult,
)
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.proc import run_process


class FakeGameMaster:
    def __init__(
        self,
        scenario: Mapping[str, Any],
        *,
        clock: Clock | None = None,
        team_repo: Path | None = None,
        grade_async: bool = False,
    ) -> None:
        self._sc = dict(scenario)
        self._clock = clock or SystemClock()
        self._repo = team_repo
        self._grade_async = grade_async
        self._lock = threading.RLock()
        self._started_at: datetime | None = None
        self._paused_at: datetime | None = None
        self._paused_total = timedelta()
        self._status = MatchStatus.READY
        self._working: set[str] = set()
        self._subs: dict[str, dict[str, Any]] = {}  # item_id → {key, body, result, grade}
        self.calls: list[tuple[str, str]] = []  # (api, item) — test kiểm luật
        self.grader_logs: dict[str, list[str]] = {}  # local simulator diagnostics only

    @classmethod
    def from_file(cls, path: Path, **kw: Any) -> FakeGameMaster:
        return cls(json.loads(path.read_text(encoding=ENCODING)), **kw)

    @property
    def match_id(self) -> str:
        return str(self._sc.get("match_id", "M-PRACTICE"))

    # ───── điều khiển trận (Admin trong thực tế) ─────
    def start_match(self) -> None:
        with self._lock:
            self._started_at = self._clock.now()
            self._status = MatchStatus.IN_PROGRESS

    def pause(self) -> None:
        with self._lock:
            if self._status is MatchStatus.IN_PROGRESS:
                self._paused_at = self._clock.now()
                self._status = MatchStatus.PAUSED

    def resume(self) -> None:
        with self._lock:
            if self._status is MatchStatus.PAUSED and self._paused_at is not None:
                self._paused_total += self._clock.now() - self._paused_at
                self._paused_at = None
                self._status = MatchStatus.IN_PROGRESS

    # ───── đồng hồ trận (không tính PAUSED) ─────
    def _elapsed_s(self) -> float:
        if self._started_at is None:
            return -1.0
        end = self._paused_at or self._clock.now()
        return (end - self._started_at - self._paused_total).total_seconds()

    def _status_now(self) -> MatchStatus:
        if (
            self._status is MatchStatus.IN_PROGRESS
            and self._elapsed_s() >= float(self._sc.get("duration_min", 40)) * 60
        ):
            self._status = MatchStatus.FINISHED
        return self._status

    def _wall(self, match_s: float) -> datetime:
        """Mốc giờ server tương ứng với giây trận `match_s` (giả định không còn pause trong tương lai)."""
        assert self._started_at is not None  # noqa: S101
        return self._started_at + self._paused_total + timedelta(seconds=match_s)

    def _spec(self, item_id: str) -> dict[str, Any]:
        for it in self._sc.get("items", []):
            if it.get("id") == item_id:
                return dict(it)
        raise GameMasterError(f"item {item_id} không tồn tại", status=404)

    def _visible(self, it: Mapping[str, Any]) -> bool:
        return self._started_at is not None and self._elapsed_s() >= float(it.get("reveal_at_s", 0))

    def _item(self, it: Mapping[str, Any]) -> BacklogItem:
        el = self._elapsed_s()
        reveal_s = float(it.get("reveal_at_s", 0))
        description = str(it.get("description", ""))
        for ch in it.get("changes", []):  # yêu cầu thay đổi giữa trận
            if el >= float(ch.get("at_s", 0)):
                description = str(ch.get("description", description))
        deadline_min = it.get("deadline_min")
        closes_s = reveal_s + float(deadline_min) * 60 if deadline_min is not None else None
        status = ItemStatus.OPEN
        if closes_s is not None and el >= closes_s:
            status = ItemStatus.CLOSED
        return BacklogItem(
            id=str(it["id"]),
            type=ItemType(str(it.get("type", str(it["id"]).split("-")[0])).upper()),
            title=str(it.get("title", "")),
            description=description,
            status=status,
            points=float(it["points"]) if it.get("points") is not None else None,
            revealed_at=self._wall(reveal_s),
            deadline_at=self._wall(closes_s) if closes_s is not None else None,
        )

    # ───── API ─────
    def snapshot(self) -> MatchSnapshot:
        with self._lock:
            self.calls.append(("snapshot", ""))
            status = self._status_now()
            items = tuple(self._item(it) for it in self._sc.get("items", []) if self._visible(it))
            return MatchSnapshot(
                match_id=self.match_id,
                status=status,
                server_time=self._clock.now(),
                ends_at=self._wall(float(self._sc.get("duration_min", 40)) * 60)
                if self._started_at
                else None,
                brief=dict(self._sc.get("brief", {})),
                repository=dict(self._sc.get("repository", {})),
                items=items,
                exam_info=dict(self._sc.get("exam_info", {})),
            )

    def start_working(self, item_id: str) -> StartResult:
        with self._lock:
            self.calls.append(("start_working", item_id))
            if self._status_now() is not MatchStatus.IN_PROGRESS:
                raise GameMasterError("Match is not in progress", status=409)
            it = self._spec(item_id)
            if not self._visible(it):
                raise GameMasterError(f"{item_id} chưa mở", status=404)  # luật: không lấy được đề/MVS chưa mở
            if self._item(it).status is ItemStatus.CLOSED:
                raise GameMasterError(f"{item_id} đã CLOSED", status=409)
            self._working.add(item_id)
            mvs = it.get("mvs", []) if ItemType(str(it.get("type", "TASK")).upper()).has_mvs else []
            return StartResult(
                item_id=item_id, submit_template=dict(it.get("submit_template", {})), mvs=tuple(mvs)
            )

    def submit(
        self, item_id: str, *, git_revision: str, answer: dict[str, Any], idempotency_key: str
    ) -> SubmitResult:
        with self._lock:
            self.calls.append(("submit", item_id))
            body = json.dumps({"gitRevision": git_revision, "answer": answer}, sort_keys=True)
            prev = self._subs.get(item_id)
            if prev is not None:
                if prev["key"] == idempotency_key and prev["body"] == body:
                    return prev["result"]  # type: ignore[no-any-return]
                raise GameMasterError(f"{item_id} đã nộp — mỗi item chỉ nộp 1 lần", status=409)
            if item_id not in self._working:
                raise GameMasterError(f"{item_id}: chưa start working → từ chối", status=409)
            if self._status_now() is not MatchStatus.IN_PROGRESS:
                raise GameMasterError("Match is not in progress", status=409)
            if self._item(self._spec(item_id)).status is ItemStatus.CLOSED:
                raise GameMasterError(f"{item_id} đã CLOSED", status=409)
            if len(git_revision) != 40:
                raise GameMasterError("gitRevision phải là full SHA (40 ký tự)", status=422)
            sid = f"S-{item_id}-1"
            result = SubmitResult(item_id=item_id, submission_id=sid, status="ACCEPTED")
            self._subs[item_id] = {"key": idempotency_key, "body": body, "result": result, "grade": None}
        if self._grade_async:
            threading.Thread(target=self._grade, args=(item_id, git_revision), daemon=True).start()
        else:
            self._grade(item_id, git_revision)
        return result

    def submission(self, item_id: str, submission_id: str) -> GradeResult:
        with self._lock:
            self.calls.append(("submission", item_id))
            sub = self._subs.get(item_id)
            if sub is None or sub["result"].submission_id != submission_id:
                raise GameMasterError(f"submission {submission_id} không có", status=404)
            grade = sub["grade"] or (GradeStatus.PENDING, None)
            return GradeResult(item_id=item_id, submission_id=submission_id, status=grade[0], score=grade[1])

    # ───── Grader ─────
    def _grade(self, item_id: str, git_revision: str) -> None:
        it = self._spec(item_id)
        ok = self._run_grader(it.get("grader") or {}, git_revision)
        score = float(it.get("points") or 0) if ok else 0.0
        with self._lock:
            self._subs[item_id]["grade"] = (GradeStatus.PASSED if ok else GradeStatus.FAILED, score)

    def _run_grader(self, grader: Mapping[str, Any], rev: str) -> bool:
        cmd = grader.get("cmd")
        if not cmd or self._repo is None:
            return True  # không có lệnh chấm → coi như đạt (kịch bản chỉ kiểm luồng)
        tmp = Path(tempfile.mkdtemp(prefix="fakegm-"))
        wt = tmp / "wt"

        def run(argv: list[str], cwd: Path, timeout_s: float = 120.0) -> int:
            # Windows CreateProcess may otherwise find the base interpreter before the venv on PATH.
            argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
            r = run_process(
                argv,
                cwd=cwd,
                env=dict(os.environ),
                timeout_s=timeout_s,
                stdout_path=tmp / "out.txt",
                stderr_path=tmp / "err.txt",
                cancel=CancelToken(),
            )
            logs = [
                p.read_text(encoding=ENCODING, errors="replace")
                for p in (tmp / "out.txt", tmp / "err.txt")
                if p.exists()
            ]
            self.grader_logs.setdefault(rev, []).append(f"{argv!r}: exit={r.exit_code}\n" + "\n".join(logs))
            return r.exit_code if not r.timed_out else -1

        try:
            if run(["git", "-C", str(self._repo), "worktree", "add", "--detach", str(wt), rev], tmp) != 0:
                return False
            for rel, text in (grader.get("files") or {}).items():  # test ẩn của Grader
                p = wt / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(text), encoding=ENCODING)
            return (
                run(shlex.split(str(cmd), posix=os.name != "nt"), wt / str(grader.get("cwd", ".")), 300.0)
                == 0
            )
        except OSError:
            return False
        finally:
            run(["git", "-C", str(self._repo), "worktree", "remove", "--force", str(wt)], tmp)
            with contextlib.suppress(OSError):
                shutil.rmtree(tmp, ignore_errors=True)

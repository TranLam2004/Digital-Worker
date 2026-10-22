"""Queue — bảng `queue` SQLite. Ưu tiên: P0 security > P0 > P1 > P2 > P3 (fast/complexity bổ sung sau TRIAGE, P2).

priority số: thấp = trước. P0=0, P1=10, P2=20, P3=30; nhãn security −5; tie-break theo enqueued_at.
blocked_by: issue DDT-xxxx chưa done → bỏ qua, lấy issue sau.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.core.models import Issue
from digital_development_team.storage.db import Database
from digital_development_team.util import jsonio

_SECURITY_LABELS = {"security", "vulnerability", "cve", "sec"}
_PRIO = {"P0": 0, "P1": 10, "P2": 20, "P3": 30}


def priority_of(issue: Issue) -> int:
    base = _PRIO.get(issue.priority_hint or "", 20)
    if any(label.lower() in _SECURITY_LABELS for label in issue.labels):
        base -= 5
    return base


@dataclass(frozen=True, slots=True)
class QueueRow:
    issue_id: str
    priority: int
    status: str
    source: str
    source_id: str
    title: str
    blocked_by: tuple[str, ...]
    enqueued_at: str


class Queue:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def find_by_source(self, source: str, source_id: str) -> str | None:
        row = self._db.one("SELECT issue_id FROM queue WHERE source=? AND source_id=?", (source, source_id))
        return str(row["issue_id"]) if row else None

    def last_issue_id(self) -> str | None:
        row = self._db.one("SELECT issue_id FROM queue ORDER BY issue_id DESC LIMIT 1")
        return str(row["issue_id"]) if row else None

    def enqueue(self, issue: Issue) -> QueueRow:
        now = self._clock.now().isoformat()
        with self._db.transaction() as c:
            c.execute(
                "INSERT INTO queue(issue_id, priority, status, source, source_id, title, blocked_by, enqueued_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    issue.id,
                    priority_of(issue),
                    "queued",
                    issue.source,
                    issue.source_id,
                    issue.title,
                    jsonio.dumps(list(issue.blocked_by), indent=None),
                    now,
                ),
            )
        row = self.get(issue.id)
        assert row is not None  # noqa: S101 - vừa insert xong
        return row

    def get(self, issue_id: str) -> QueueRow | None:
        row = self._db.one("SELECT * FROM queue WHERE issue_id=?", (issue_id,))
        return self._row(row) if row else None

    def list(self, status: str | None = None) -> list[QueueRow]:
        sql, params = "SELECT * FROM queue", ()
        if status:
            sql, params = sql + " WHERE status=?", (status,)  # type: ignore[assignment]
        return [self._row(r) for r in self._db.all(sql + " ORDER BY priority, enqueued_at, issue_id", params)]

    _STATUS_SQL: ClassVar[dict[str, str]] = {
        "running": "UPDATE queue SET status=?, started_at=? WHERE issue_id=?",
        "done": "UPDATE queue SET status=?, finished_at=? WHERE issue_id=?",
        "blocked": "UPDATE queue SET status=?, finished_at=? WHERE issue_id=?",
        "superseded": "UPDATE queue SET status=?, finished_at=? WHERE issue_id=?",
    }

    def set_status(self, issue_id: str, status: str) -> None:
        sql = self._STATUS_SQL.get(status)
        with self._db.transaction() as c:
            if sql:
                c.execute(sql, (status, self._clock.now().isoformat(), issue_id))
            else:
                c.execute("UPDATE queue SET status=? WHERE issue_id=?", (status, issue_id))

    def set_priority(self, issue_id: str, priority: int) -> None:
        """Thấp = chạy trước. Người đổi từ web qua hàng lệnh, worker áp."""
        with self._db.transaction() as c:
            c.execute("UPDATE queue SET priority=? WHERE issue_id=?", (int(priority), issue_id))

    def replace_blocker(self, old: str, new: Sequence[str]) -> tuple[str, ...]:
        """Issue đang chờ `old` → chờ mọi issue trong `new` (issue cha đã tách thành con). Trả issue đã đổi."""
        moved: builtins.list[str] = []
        with self._db.transaction() as c:
            for r in c.execute("SELECT issue_id, blocked_by FROM queue WHERE status='queued'").fetchall():
                deps = [str(d) for d in jsonio.loads(r["blocked_by"])]
                if old not in deps or r["issue_id"] in new:
                    continue
                deps = [d for d in deps if d != old] + [x for x in new if x not in deps]
                c.execute(
                    "UPDATE queue SET blocked_by=? WHERE issue_id=?",
                    (jsonio.dumps(deps, indent=None), r["issue_id"]),
                )
                moved.append(str(r["issue_id"]))
        return tuple(moved)

    def done_ids(self) -> set[str]:
        return {
            str(r["issue_id"])
            for r in self._db.all("SELECT issue_id FROM queue WHERE status IN ('done','superseded')")
        }

    def next(self) -> QueueRow | None:
        """Issue queued ưu tiên nhất mà mọi blocked_by đã done/superseded."""
        done = self.done_ids()
        for row in self.list("queued"):
            if all(b in done for b in row.blocked_by):
                return row
        return None

    def counts(self) -> dict[str, int]:
        return {
            str(r["status"]): int(r["n"])
            for r in self._db.all("SELECT status, COUNT(*) n FROM queue GROUP BY status")
        }

    @staticmethod
    def _row(r: object) -> QueueRow:
        return QueueRow(
            issue_id=r["issue_id"],  # type: ignore[index]
            priority=int(r["priority"]),  # type: ignore[index]
            status=r["status"],  # type: ignore[index]
            source=r["source"],  # type: ignore[index]
            source_id=r["source_id"],  # type: ignore[index]
            title=r["title"],  # type: ignore[index]
            blocked_by=tuple(jsonio.loads(r["blocked_by"])),  # type: ignore[index]
            enqueued_at=r["enqueued_at"],  # type: ignore[index]
        )


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)

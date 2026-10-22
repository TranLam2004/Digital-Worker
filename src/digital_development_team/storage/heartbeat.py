"""Heartbeat của `ddt worker` trong SQLite (bảng worker_heartbeat).

Worker beat mỗi ≤1s từ thread watcher. Web/CLI đọc để trả lời: worker còn sống không, đang làm issue nào, stage nào.
Checkpoint không có PID: runner chết đột ngột để lại board `running` mãi — heartbeat là cách duy nhất phân biệt.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime

from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.storage.db import Database

DEAD_AFTER_FACTOR = 3.0
"""Quá 3 lần poll không beat → coi là chết."""


@dataclass(frozen=True, slots=True)
class HeartbeatRow:
    worker_id: str
    pid: int
    host: str
    started_at: str
    ts: str
    state: str
    current_issue: str | None
    current_stage: str | None
    loop_n: int
    poll_s: float

    def age_s(self, now: datetime) -> float:
        return (now - datetime.fromisoformat(self.ts)).total_seconds()

    def alive(self, now: datetime, *, factor: float = DEAD_AFTER_FACTOR) -> bool:
        return self.state != "exited" and self.age_s(now) <= max(self.poll_s, 0.5) * factor


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Heartbeat:
    def __init__(
        self, db: Database, clock: Clock | None = None, *, worker_id: str | None = None, poll_s: float = 2.0
    ) -> None:
        self._db = db
        self._clock = clock or SystemClock()
        self.worker_id = worker_id or default_worker_id()
        self._poll_s = poll_s
        self._started_at = self._clock.now().isoformat()

    def beat(
        self,
        state: str,
        *,
        current_issue: str | None = None,
        current_stage: str | None = None,
        loop_n: int = 0,
    ) -> None:
        with self._db.transaction() as c:
            c.execute(
                "INSERT INTO worker_heartbeat(worker_id, pid, host, started_at, ts, state, current_issue, current_stage, loop_n, poll_s)"
                " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET"
                " ts=excluded.ts, state=excluded.state, current_issue=excluded.current_issue,"
                " current_stage=excluded.current_stage, loop_n=excluded.loop_n, poll_s=excluded.poll_s",
                (
                    self.worker_id,
                    os.getpid(),
                    socket.gethostname(),
                    self._started_at,
                    self._clock.now().isoformat(),
                    state,
                    current_issue,
                    current_stage,
                    loop_n,
                    self._poll_s,
                ),
            )

    def exited(self) -> None:
        """Thoát sạch: giữ dòng với state=exited để web hiện 'worker đã thoát lúc …'."""
        self.beat("exited")

    def remove(self) -> None:
        with self._db.transaction() as c:
            c.execute("DELETE FROM worker_heartbeat WHERE worker_id=?", (self.worker_id,))


def list_heartbeats(db: Database) -> list[HeartbeatRow]:
    rows = db.all("SELECT * FROM worker_heartbeat ORDER BY ts DESC")
    return [
        HeartbeatRow(
            worker_id=r["worker_id"],
            pid=int(r["pid"]),
            host=r["host"],
            started_at=r["started_at"],
            ts=r["ts"],
            state=r["state"],
            current_issue=r["current_issue"],
            current_stage=r["current_stage"],
            loop_n=int(r["loop_n"]),
            poll_s=float(r["poll_s"]),
        )
        for r in rows
    ]


def live_workers(db: Database, now: datetime) -> list[HeartbeatRow]:
    return [h for h in list_heartbeats(db) if h.alive(now)]

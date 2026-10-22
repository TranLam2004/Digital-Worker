"""Hàng lệnh người → worker (migration 0004). Web ghi `pending`, worker áp ở tick kế rồi ghi `done`/`failed`.

Giữ nguyên tắc một writer: events.jsonl, checkpoint, blackboard chỉ do worker ghi. Web chỉ thêm dòng vào bảng này.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from digital_development_team.storage.db import Database
from digital_development_team.util import jsonio

KINDS = frozenset(
    {"ingest", "set_priority", "cancel", "retry", "reopen", "backfill", "worker_stop", "mark_bad_merge"}
)
"""Loại lệnh worker hiểu. Lệnh lạ bị từ chối lúc gửi, không nằm chờ mãi."""


@dataclass(frozen=True, slots=True)
class ControlRequest:
    id: int
    kind: str
    issue_id: str | None
    payload: dict[str, Any]
    requested_by: str
    requested_at: str
    status: str
    result: dict[str, Any] | None
    done_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "issue_id": self.issue_id,
            "payload": self.payload,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "status": self.status,
            "result": self.result,
            "done_at": self.done_at,
        }


class ControlQueue:
    def __init__(self, db: Database) -> None:
        self._db = db

    def submit(
        self,
        kind: str,
        *,
        by: str,
        at: datetime,
        issue: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        if kind not in KINDS:
            raise ValueError(f"lệnh lạ: {kind!r}")
        with self._db.transaction() as c:
            cur = c.execute(
                "INSERT INTO control_requests(kind, issue_id, payload, requested_by, requested_at) VALUES(?,?,?,?,?)",
                (kind, issue, jsonio.dumps(dict(payload or {}), indent=None), by, at.isoformat()),
            )
            return int(cur.lastrowid or 0)

    def pending(self, *, kind: str | None = None, issue: str | None = None) -> list[ControlRequest]:
        sql = "SELECT * FROM control_requests WHERE status='pending'"
        params: list[Any] = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if issue:
            sql += " AND issue_id=?"
            params.append(issue)
        return [self._row(r) for r in self._db.all(sql + " ORDER BY id", params)]

    def recent(self, *, limit: int = 50) -> list[ControlRequest]:
        return [
            self._row(r)
            for r in self._db.all("SELECT * FROM control_requests ORDER BY id DESC LIMIT ?", (limit,))
        ]

    def complete(self, req_id: int, *, ok: bool, result: Mapping[str, Any], at: datetime) -> None:
        with self._db.transaction() as c:
            c.execute(
                "UPDATE control_requests SET status=?, result=?, done_at=? WHERE id=? AND status='pending'",
                ("done" if ok else "failed", jsonio.dumps(dict(result), indent=None), at.isoformat(), req_id),
            )

    @staticmethod
    def _row(r: Any) -> ControlRequest:
        return ControlRequest(
            id=int(r["id"]),
            kind=str(r["kind"]),
            issue_id=r["issue_id"],
            payload=dict(jsonio.loads(r["payload"] or "{}")),
            requested_by=str(r["requested_by"]),
            requested_at=str(r["requested_at"]),
            status=str(r["status"]),
            result=dict(jsonio.loads(r["result"])) if r["result"] else None,
            done_at=r["done_at"],
        )


__all__ = ["KINDS", "ControlQueue", "ControlRequest"]

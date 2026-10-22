"""GateService — gate_requests trong SQLite. Hai bên:

- Runner (stage *_WAIT): open() idempotent → poll() mỗi vòng → nhận GateDecision (người hoặc timeout).
- CLI/web (`ddt resolve`): resolve(). KHÔNG emit event — chỉ runner emit gate_resolved khi poll thấy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.core.enums import GateId
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import GateDecision, GateRequest, GateTicket
from digital_development_team.storage.db import Database
from digital_development_team.util import jsonio

TIMEOUT_CHOICE = "timeout"


@dataclass(frozen=True, slots=True)
class OpenGate:
    ticket: GateTicket
    tier: str
    summary: dict[str, object]
    escalation_uri: str | None
    status: str
    choice: str | None
    decided_by: str | None
    note: str | None

    @property
    def id(self) -> str:
        return self.ticket.id


def ticket_id(issue: str, gate: GateId, attempt: int) -> str:
    return f"{issue}:{gate.value}:{attempt}"


class GateService:
    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def open(self, req: GateRequest) -> GateTicket:
        tid = ticket_id(req.issue_id, req.gate, req.attempt)
        row = self._db.one("SELECT * FROM gate_requests WHERE id=?", (tid,))
        if row is not None:
            return self._ticket(row)
        now = self._clock.now()
        with self._db.transaction() as c:
            c.execute(
                "INSERT INTO gate_requests(id, issue_id, gate, attempt, tier, summary, escalation_uri, opened_at, timeout_s)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    req.issue_id,
                    req.gate.value,
                    req.attempt,
                    req.tier.value,
                    jsonio.dumps(req.summary, indent=None),
                    req.escalation_uri,
                    now.isoformat(),
                    req.timeout_s,
                ),
            )
        return GateTicket(
            id=tid, issue_id=req.issue_id, gate=req.gate, opened_at=now, timeout_s=req.timeout_s
        )

    def poll(self, ticket: GateTicket, *, now: datetime | None = None) -> GateDecision | None:
        row = self._db.one("SELECT * FROM gate_requests WHERE id=?", (ticket.id,))
        if row is None:
            raise StoreError(f"gate ticket {ticket.id} không tồn tại")
        if row["status"] != "open":
            return self._decision(row)
        now = now or self._clock.now()
        timeout = row["timeout_s"]
        if timeout is not None and (now - ticket.opened_at).total_seconds() >= float(timeout):
            decision = GateDecision(
                choice=TIMEOUT_CHOICE, by="system", at=now, note="timeout", is_timeout=True
            )
            self._write_decision(ticket.id, decision, status="timeout")
            return decision
        return None

    def resolve(self, ticket_id_: str, decision: GateDecision) -> None:
        row = self._db.one("SELECT status FROM gate_requests WHERE id=?", (ticket_id_,))
        if row is None:
            raise StoreError(f"gate {ticket_id_!r} không tồn tại", hint="ddt gates để xem gate đang mở")
        if row["status"] != "open":
            raise StoreError(f"gate {ticket_id_} đã {row['status']}")
        self._write_decision(ticket_id_, decision, status="resolved")

    def list_open(self, issue: str | None = None) -> list[OpenGate]:
        sql = "SELECT * FROM gate_requests WHERE status='open'"
        params: tuple[str, ...] = ()
        if issue:
            sql += " AND issue_id=?"
            params = (issue,)
        return [self._open_gate(r) for r in self._db.all(sql + " ORDER BY opened_at", params)]

    def get(self, ticket_id_: str) -> OpenGate | None:
        row = self._db.one("SELECT * FROM gate_requests WHERE id=?", (ticket_id_,))
        return self._open_gate(row) if row else None

    def open_for(self, issue: str, gate: GateId) -> OpenGate | None:
        row = self._db.one(
            "SELECT * FROM gate_requests WHERE issue_id=? AND gate=? AND status='open' ORDER BY attempt DESC LIMIT 1",
            (issue, gate.value),
        )
        return self._open_gate(row) if row else None

    # ───── nội bộ ─────
    def _write_decision(self, tid: str, d: GateDecision, *, status: str) -> None:
        with self._db.transaction() as c:
            c.execute(
                "UPDATE gate_requests SET status=?, choice=?, decided_by=?, note=?, resolved_at=? WHERE id=?",
                (status, d.choice, d.by, d.note, d.at.isoformat(), tid),
            )

    @staticmethod
    def _ticket(row: object) -> GateTicket:
        r = row  # sqlite3.Row
        return GateTicket(
            id=r["id"],  # type: ignore[index]
            issue_id=r["issue_id"],  # type: ignore[index]
            gate=GateId(r["gate"]),  # type: ignore[index]
            opened_at=datetime.fromisoformat(r["opened_at"]),  # type: ignore[index]
            timeout_s=r["timeout_s"],  # type: ignore[index]
        )

    @staticmethod
    def _decision(row: object) -> GateDecision:
        r = row
        return GateDecision(
            choice=r["choice"],  # type: ignore[index]
            by=r["decided_by"] or "system",  # type: ignore[index]
            at=datetime.fromisoformat(r["resolved_at"]),  # type: ignore[index]
            note=r["note"],  # type: ignore[index]
            is_timeout=r["status"] == "timeout",  # type: ignore[index]
        )

    def _open_gate(self, row: object) -> OpenGate:
        r = row
        return OpenGate(
            ticket=self._ticket(r),
            tier=r["tier"],  # type: ignore[index]
            summary=jsonio.loads(r["summary"]),  # type: ignore[index]
            escalation_uri=r["escalation_uri"],  # type: ignore[index]
            status=r["status"],  # type: ignore[index]
            choice=r["choice"],  # type: ignore[index]
            decided_by=r["decided_by"],  # type: ignore[index]
            note=r["note"],  # type: ignore[index]
        )

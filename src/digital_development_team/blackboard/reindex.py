"""Dựng lại index SQLite từ filesystem (fs là sự thật). written_by/at lấy từ events.jsonl (slot_written).

Dùng khi ddt.db hỏng/mất hoặc index lệch fs. Không đụng episodes/worker_stats (memory thật, không dựng lại được).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from digital_development_team.blackboard.checkpoints import CheckpointStore
from digital_development_team.core.enums import EventType
from digital_development_team.core.errors import CheckpointError
from digital_development_team.eventlog.reader import iter_events
from digital_development_team.schema.registry import SchemaRegistry
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout, parse_versioned_name
from digital_development_team.util import jsonio, yamlio
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.hashing import sha256_text


@dataclass
class ReindexReport:
    issues: int = 0
    slots: int = 0
    from_events: int = 0  # dòng có written_by/at phục hồi từ events
    schema_fail: list[str] = field(default_factory=list)
    checkpoint_bad: list[str] = field(default_factory=list)
    seq_reset: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.issues} issue · {self.slots} slot ({self.from_events} có provenance từ events) · "
            f"{len(self.schema_fail)} schema fail · {len(self.checkpoint_bad)} checkpoint hỏng"
        )


def reindex(layout: HomeLayout, db: Database, registry: SchemaRegistry | None = None) -> ReindexReport:
    rep = ReindexReport()
    cps = CheckpointStore(layout, db)
    for issue in layout.issue_ids():
        rep.issues += 1
        idir = layout.issue_dir(issue)
        events = list(iter_events(layout.events_path(issue)))
        provenance = {
            (e.data.get("slot"), e.data.get("version")): (e.actor, e.ts.isoformat())
            for e in events
            if e.event is EventType.SLOT_WRITTEN
        }
        if events:
            nxt = max(e.seq for e in events) + 1
            db.set_seq(issue, nxt)
            rep.seq_reset[issue] = nxt

        rows: list[tuple[str, str, int, str, str, str, str, int]] = []
        for p in sorted(idir.iterdir()):
            parsed = parse_versioned_name(p.name) if p.is_file() else None
            if parsed is None:
                continue
            slot, version = parsed
            text = p.read_text(encoding=ENCODING)
            sha = sha256_text(text)
            who, when = provenance.get((slot, version), (None, None))
            if who is not None:
                rep.from_events += 1
            else:
                who = "reindex"
                when = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat()
            schema_ok = 1
            if registry is not None and slot != "patch/":
                try:
                    data = yamlio.loads(text, source=str(p)) if p.suffix == ".yaml" else jsonio.loads(text)
                    registry.validate_slot(slot, data)
                except Exception as e:  # báo cáo, không dừng reindex
                    schema_ok = 0
                    rep.schema_fail.append(f"{issue}/{slot}@{version}: {e}")
            rows.append((issue, slot, version, layout.rel(p), sha, who, when or "", schema_ok))
        with db.transaction() as c:
            c.execute("DELETE FROM slots WHERE issue_id=?", (issue,))
            c.executemany(
                "INSERT INTO slots(issue_id, slot, version, path, sha256, written_by, written_at, schema_ok) "
                "VALUES(?,?,?,?,?,?,?,?)",
                rows,
            )
        rep.slots += len(rows)
        try:
            cp = cps.load(issue)
        except CheckpointError as e:
            rep.checkpoint_bad.append(f"{issue}: {e}")
            cp = None
        if cp is not None:
            cps.upsert_board(cp)
    return rep

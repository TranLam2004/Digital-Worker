"""Episodic memory: mỗi issue kết thúc một dòng `episodes` (memory/outcomes.py ghi), Learner thêm `lesson`.

Tìm bằng FTS5 (episodes_fts, migration 0001) trên approach · lesson · failure_reason · files. Không vector DB.
"""

from __future__ import annotations

import json
import re
from typing import Any

from digital_development_team.storage.db import Database

_TERM = re.compile(r"[A-Za-z0-9_]{2,}")


def _fts_query(text: str, *, max_terms: int = 8) -> str | None:
    terms = list(dict.fromkeys(t.lower() for t in _TERM.findall(text)))[:max_terms]
    return " OR ".join(f'"{t}"' for t in terms) if terms else None


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    try:
        d["files"] = json.loads(d.get("files") or "[]")
    except json.JSONDecodeError:
        d["files"] = []
    return d


class EpisodeStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, issue_id: str) -> dict[str, Any] | None:
        r = self._db.one(
            "SELECT id, issue_id, issue_type, module, files, approach, outcome, rework_count, failure_reason, lesson,"
            " human_correction, created_at FROM episodes WHERE issue_id=? ORDER BY id DESC LIMIT 1",
            (issue_id,),
        )
        return _row(r) if r is not None else None

    def set_lesson(self, issue_id: str, lesson: str, *, failure_reason: str | None = None) -> bool:
        ep = self.get(issue_id)
        if ep is None:
            return False
        with self._db.transaction() as c:
            c.execute(
                "UPDATE episodes SET lesson=?, failure_reason=COALESCE(?, failure_reason) WHERE id=?",
                (lesson, failure_reason, ep["id"]),
            )
        return True

    def search(
        self, query: str, *, module: str | None = None, issue_type: str | None = None, k: int = 5
    ) -> list[dict[str, Any]]:
        q = _fts_query(query)
        if q is None:
            rows = self._db.all(
                "SELECT id, issue_id, issue_type, module, files, approach, outcome, rework_count, failure_reason,"
                " lesson, human_correction, created_at FROM episodes"
                " WHERE (? IS NULL OR module=?) AND (? IS NULL OR issue_type=?) ORDER BY id DESC LIMIT ?",
                (module, module, issue_type, issue_type, k),
            )
        else:
            rows = self._db.all(
                "SELECT e.id, e.issue_id, e.issue_type, e.module, e.files, e.approach, e.outcome, e.rework_count,"
                " e.failure_reason, e.lesson, e.human_correction, e.created_at"
                " FROM episodes_fts f JOIN episodes e ON e.id = f.rowid WHERE episodes_fts MATCH ?"
                " AND (? IS NULL OR e.module=?) AND (? IS NULL OR e.issue_type=?) ORDER BY bm25(episodes_fts) LIMIT ?",
                (q, module, module, issue_type, issue_type, k),
            )
        return [_row(r) for r in rows]

    def related(self, module: str, issue_type: str, *, exclude: str, k: int) -> list[dict[str, Any]]:
        """Episode có lesson cùng module (cùng loại trước), người từng sửa trước, mới trước."""
        rows = self._db.all(
            "SELECT id, issue_id, issue_type, module, files, approach, outcome, rework_count, failure_reason, lesson,"
            " human_correction, created_at FROM episodes"
            " WHERE module=? AND issue_id != ? AND lesson IS NOT NULL AND lesson != ''"
            " ORDER BY (issue_type = ?) DESC, human_correction DESC, id DESC LIMIT ?",
            (module, exclude, issue_type, k),
        )
        return [_row(r) for r in rows]


__all__ = ["EpisodeStore"]

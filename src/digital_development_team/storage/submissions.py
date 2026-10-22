"""Durable single-submission ledger shared by every issue version and process.

Reserve before sending. A timeout or crash never permits replacing the request.
Concurrent retries may send the same idempotent request; acceptance is monotonic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from digital_development_team.core.errors import StoreError
from digital_development_team.storage.db import Database


class SubmissionLedger:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, match_id: str, item_id: str) -> dict[str, Any] | None:
        row = self._db.one(
            "SELECT issue_id, request_json, result_json FROM match_submissions WHERE match_id=? AND item_id=?",
            (match_id, item_id),
        )
        if row is None:
            return None
        return {
            **json.loads(row["request_json"]),
            **json.loads(row["result_json"]),
            "issue_id": row["issue_id"],
        }

    def reserve(
        self, match_id: str, item_id: str, issue_id: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._db.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO match_submissions(match_id,item_id,issue_id,request_json) VALUES(?,?,?,?)",
                (match_id, item_id, issue_id, json.dumps(dict(request), sort_keys=True, ensure_ascii=False)),
            )
        record = self.get(match_id, item_id)
        if record is None:
            raise StoreError("Submission reservation was not persisted")
        return record

    def record(self, match_id: str, item_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"status", "submission_id", "provisional_score", "grade", "score", "error", "http_status"}
        changes = {k: v for k, v in result.items() if k in allowed}
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT result_json FROM match_submissions WHERE match_id=? AND item_id=?",
                (match_id, item_id),
            ).fetchone()
            if row is None:
                raise StoreError("Cannot record an unreserved submission")
            prior = json.loads(row[0])
            if prior.get("status") == "ACCEPTED" and changes.get("status", "ACCEPTED") != "ACCEPTED":
                changes = {}
            if prior.get("grade") in ("PASSED", "FAILED"):
                changes.pop("grade", None)
                changes.pop("score", None)
            c.execute(
                "UPDATE match_submissions SET result_json=? WHERE match_id=? AND item_id=?",
                (json.dumps({**prior, **changes}, ensure_ascii=False), match_id, item_id),
            )
        record = self.get(match_id, item_id)
        if record is None:
            raise StoreError("Submission record disappeared")
        return record

    def issues(self, match_id: str) -> list[str]:
        return [
            str(r[0])
            for r in self._db.all("SELECT issue_id FROM match_submissions WHERE match_id=?", (match_id,))
        ]

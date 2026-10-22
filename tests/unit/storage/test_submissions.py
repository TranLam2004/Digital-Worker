from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from digital_development_team.storage.db import Database
from digital_development_team.storage.submissions import SubmissionLedger


def test_parallel_reservations_and_restart_keep_one_request(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    db = Database(path)
    ledger = SubmissionLedger(db)

    def reserve(i: int) -> dict:
        return ledger.reserve(
            "M",
            "BUG-1",
            f"DDT-{i:04d}",
            {
                "git_revision": str(i) * 40,
                "idempotency_key": str(i),
                "answer": {"summary": str(i)},
            },
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(reserve, range(1, 5)))
    assert all(record == records[0] for record in records)
    db.close()
    restarted = Database(path)
    try:
        assert SubmissionLedger(restarted).get("M", "BUG-1") == records[0]
        assert SubmissionLedger(restarted).get("OTHER", "BUG-1") is None
    finally:
        restarted.close()


def test_late_timeout_cannot_erase_receipt_or_terminal_grade(tmp_path: Path) -> None:
    db = Database(tmp_path / "ledger.db")
    try:
        ledger = SubmissionLedger(db)
        ledger.reserve(
            "M", "BUG-1", "DDT-0001", {"answer": {}, "git_revision": "a" * 40, "idempotency_key": "k"}
        )
        ledger.record("M", "BUG-1", {"status": "ACCEPTED", "submission_id": "S", "grade": "PENDING"})
        ledger.record("M", "BUG-1", {"grade": "FAILED", "score": 0})
        ledger.record("M", "BUG-1", {"status": "UNKNOWN", "error": "late timeout"})
        result = ledger.record("M", "BUG-1", {"grade": "PENDING", "score": 100, "answer": {"tampered": True}})
        assert result["status"] == "ACCEPTED" and result["grade"] == "FAILED"
        assert result["score"] == 0 and result["answer"] == {}
    finally:
        db.close()

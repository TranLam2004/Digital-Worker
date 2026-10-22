from __future__ import annotations

import threading
from pathlib import Path

import pytest

from digital_development_team.core.errors import StoreError
from digital_development_team.storage import Database, HomeLayout, parse_versioned_name, slot_stem_ext


def test_migrate_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    assert db.user_version >= 1
    assert db.migrate() == 0
    tables = {r[0] for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "slots",
        "event_seq",
        "board",
        "queue",
        "locks",
        "gate_requests",
        "episodes",
        "worker_stats",
    } <= tables
    db.close()


def test_next_seq_contiguous_across_threads(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    got: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(25):
            s = db.next_seq("DDT-0001")
            with lock:
                got.append(s)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(got) == list(range(100))
    assert db.peek_seq("DDT-0001") == 100
    assert db.next_seq("DDT-0002") == 0  # counter riêng từng issue
    db.close()


def test_transaction_rollback(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    with pytest.raises(RuntimeError), db.transaction() as c:
        c.execute("INSERT INTO event_seq(issue_id, next) VALUES('a', 5)")
        raise RuntimeError("boom")
    assert db.scalar("SELECT COUNT(*) FROM event_seq") == 0
    with pytest.raises(StoreError):
        db.execute("SELECT * FROM no_such_table")
    db.close()


def test_fts5_trigger(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    db.execute(
        "INSERT INTO episodes(issue_id, issue_type, module, files, approach, outcome, created_at) "
        "VALUES('DDT-0001','bug','calc','[\"calc.py\"]','guard empty list before mean','pass','2026-10-03')"
    )
    rows = db.all("SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH 'empty'")
    assert len(rows) == 1
    db.close()


def test_layout_paths(tmp_path: Path) -> None:
    lay = HomeLayout(tmp_path / "h.ddt").ensure()
    assert lay.events_path("global") == lay.state_dir / "events.global.jsonl"
    assert lay.events_path("DDT-0001") == lay.work_dir / "DDT-0001" / "events.jsonl"
    assert lay.slot_versioned_path("DDT-0001", "spec.yaml", 2).name == "spec.v2.yaml"
    assert lay.slot_versioned_path("DDT-0001", "patch/", 1).name == "patch.v1.json"
    assert lay.slot_latest_path("DDT-0001", "patch/").name == "patch"
    assert lay.rel(lay.slot_versioned_path("DDT-0001", "spec.yaml", 2)) == "work/DDT-0001/spec.v2.yaml"
    assert lay.abs("work/DDT-0001/spec.v2.yaml") == lay.slot_versioned_path("DDT-0001", "spec.yaml", 2)
    assert lay.overrides_dir.is_dir() and lay.state_dir.is_dir()


def test_versioned_name_parsing() -> None:
    assert slot_stem_ext("spec.yaml") == ("spec", "yaml")
    assert slot_stem_ext("patch/") == ("patch", "json")
    assert parse_versioned_name("spec.v2.yaml") == ("spec.yaml", 2)
    assert parse_versioned_name("patch.v1.json") == ("patch/", 1)
    assert parse_versioned_name("spec.yaml") is None
    assert parse_versioned_name("events.jsonl") is None
    assert parse_versioned_name("checkpoint.json") is None

"""sqlite3 stdlib, wrapper mỏng (không ORM — xem README › Quyết định kiến trúc).

- 1 connection / thread (threading.local), isolation_level=None → tự quản transaction bằng BEGIN IMMEDIATE.
- WAL + busy_timeout 5s: nhiều reader, 1 writer.
- Migration: storage/migrations/NNNN_*.sql, theo dõi bằng PRAGMA user_version.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from digital_development_team.core.errors import StoreError

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for p in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            raise StoreError(f"tên migration sai: {p.name}", hint="NNNN_snake_case.sql")
        out.append((int(m.group(1)), p))
    return out


class Database:
    def __init__(self, path: Path, *, migrate: bool = True) -> None:
        self._path = path
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if migrate:
            self.migrate()

    @property
    def path(self) -> Path:
        return self._path

    # ───── connection ─────
    def conn(self) -> sqlite3.Connection:
        c: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self._path, isolation_level=None, check_same_thread=True, timeout=5.0)
            c.row_factory = sqlite3.Row
            for pragma in _PRAGMAS:
                c.execute(pragma)
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    def close(self) -> None:
        with self._conns_lock:
            for c in self._conns:
                with contextlib.suppress(sqlite3.Error):  # pragma: no cover
                    c.close()
            self._conns.clear()
        self._local = threading.local()

    # ───── transaction ─────
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE → ghi nối tiếp an toàn giữa thread/process. Lỗi → ROLLBACK + StoreError."""
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            raise StoreError(f"không mở được transaction: {e}", db=str(self._path)) from e
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    # ───── helper ─────
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn().execute(sql, params)
        except sqlite3.Error as e:
            raise StoreError(f"sqlite: {e}", sql=sql.split("\n", 1)[0][:80]) from e

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        row = self.execute(sql, params).fetchone()
        return row  # type: ignore[no-any-return]

    def all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return list(self.execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    # ───── migration ─────
    @property
    def user_version(self) -> int:
        return int(self.scalar("PRAGMA user_version") or 0)

    def migrate(self) -> int:
        """Áp mọi migration > user_version. Trả số migration đã áp."""
        applied = 0
        current = self.user_version
        for version, path in list_migrations():
            if version <= current:
                continue
            sql = path.read_text(encoding="utf-8")
            c = self.conn()
            try:
                c.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
                c.execute(f"PRAGMA user_version={version}")
            except sqlite3.Error as e:
                raise StoreError(f"migration {path.name} thất bại: {e}") from e
            log.info("migration %s áp dụng", path.name)
            applied += 1
        return applied

    # ───── counter ─────
    def next_seq(self, issue_id: str) -> int:
        """Counter atomic per issue, bắt đầu 0. Dùng cho events.jsonl."""
        with self.transaction() as c:
            row = c.execute(
                "INSERT INTO event_seq(issue_id, next) VALUES(?, 1) "
                "ON CONFLICT(issue_id) DO UPDATE SET next = next + 1 RETURNING next - 1",
                (issue_id,),
            ).fetchone()
        return int(row[0])

    def peek_seq(self, issue_id: str) -> int:
        """seq tiếp theo sẽ cấp (không tăng)."""
        return int(self.scalar("SELECT next FROM event_seq WHERE issue_id=?", (issue_id,)) or 0)

    def set_seq(self, issue_id: str, next_seq: int) -> None:
        """Reindex/replay --resume: đặt lại counter theo events.jsonl."""
        with self.transaction() as c:
            c.execute(
                "INSERT INTO event_seq(issue_id, next) VALUES(?, ?) ON CONFLICT(issue_id) DO UPDATE SET next=excluded.next",
                (issue_id, next_seq),
            )

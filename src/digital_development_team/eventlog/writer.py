"""JsonlEventSink — EventSink thật: seq từ SQLite, validate trước khi cấp seq, append + fsync."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.core.events import Event, EventDraft
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING

Validator = Callable[[dict[str, Any]], None]
"""Nhận dict event, raise SchemaError nếu sai. Inject từ schema.SchemaRegistry (layer cao hơn)."""
Listener = Callable[[Event], None]

_PROBE_TS = datetime(2000, 1, 1, tzinfo=UTC)


@contextlib.contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Khoá liên process khi append events.jsonl (transport CLI: process `ddt mcp` cũng ghi). Windows msvcrt · POSIX fcntl."""
    with lock_path.open("a+b") as lf:
        if sys.platform == "win32":
            import msvcrt

            lf.seek(0)
            for _ in range(500):  # ~5s
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    lf.seek(0)
                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - POSIX
            import fcntl

            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


log = logging.getLogger(__name__)


class JsonlEventSink:
    def __init__(
        self,
        layout: HomeLayout,
        db: Database,
        *,
        clock: Clock | None = None,
        validate: Validator | None = None,
        fsync: bool = True,
    ) -> None:
        self._layout = layout
        self._db = db
        self._clock = clock or SystemClock()
        self._validate = validate
        self._fsync = fsync
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._listeners: list[Listener] = []

    def add_listener(self, fn: Listener) -> None:
        """Fan-out trong process (web SSE, test). Lỗi listener không làm hỏng emit."""
        self._listeners.append(fn)

    def _lock_for(self, issue: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(issue)
            if lock is None:
                lock = self._locks[issue] = threading.Lock()
            return lock

    def emit(self, draft: EventDraft) -> Event:
        if self._validate is not None:
            # validate TRƯỚC khi cấp seq → lỗi không tạo lỗ hổng seq
            probe = Event.from_draft(draft, seq=0, ts=_PROBE_TS)
            self._validate(json.loads(probe.to_line()))
        path = self._layout.events_path(draft.issue_id)
        with self._lock_for(draft.issue_id):
            seq = self._db.next_seq(draft.issue_id)
            event = Event.from_draft(draft, seq=seq, ts=self._clock.now())
            path.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(path.with_suffix(".lock")), path.open("a", encoding=ENCODING, newline="\n") as f:
                f.write(event.to_line())
                f.flush()
                if self._fsync:
                    os.fsync(f.fileno())
        for fn in self._listeners:
            try:
                fn(event)
            except Exception as e:  # listener là viewer, không được làm hỏng audit log
                log.warning("event listener lỗi: %s", e)
        return event

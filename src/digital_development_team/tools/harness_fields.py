"""HarnessFields — field do harness điền vào artifact agent ghi (evidence.version · machine_gate · checklist_results ·
scope_violation). Stage đặt trước khi gọi agent; BlackboardTools.write merge (harness thắng) trước khi validate.

Có `store_dir` → mỗi `set` ghi thêm `<store_dir>/<issue>/<slot>.json` để process khác đọc được: với transport CLI,
agent ghi artifact qua MCP server (`ddt mcp`, process riêng) — bộ nhớ của runner không tới được process đó.
`get` ưu tiên bộ nhớ, thiếu thì đọc file (không cache, để luôn thấy giá trị runner vừa đặt).
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from digital_development_team.util.fsio import ENCODING, write_text_atomic


def _slot_file(slot: str) -> str:
    return slot.replace("/", "_").replace("\\", "_") + ".json"


class HarnessFields:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._fields: dict[tuple[str, str], dict[str, Any]] = {}
        self._dir = store_dir

    def _path(self, issue: str, slot: str) -> Path | None:
        return self._dir / issue / _slot_file(slot) if self._dir is not None else None

    def set(self, issue: str, slot: str, fields: Mapping[str, Any]) -> None:
        with self._lock:
            self._fields[(issue, slot)] = dict(fields)
            p = self._path(issue, slot)
            if p is not None:
                p.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(p, json.dumps(dict(fields), ensure_ascii=False, default=str), fsync=False)

    def get(self, issue: str, slot: str) -> dict[str, Any]:
        with self._lock:
            mem = self._fields.get((issue, slot))
            if mem is not None:
                return dict(mem)
            p = self._path(issue, slot)
            if p is None or not p.is_file():
                return {}
            try:
                data = json.loads(p.read_text(encoding=ENCODING))
            except (OSError, json.JSONDecodeError):
                return {}
            return dict(data) if isinstance(data, dict) else {}

    def clear(self, issue: str, slot: str | None = None) -> None:
        with self._lock:
            for key in list(self._fields):
                if key[0] == issue and (slot is None or key[1] == slot):
                    del self._fields[key]
            if self._dir is None:
                return
            d = self._dir / issue
            targets = (
                [d / _slot_file(slot)] if slot is not None else (list(d.glob("*.json")) if d.is_dir() else [])
            )
            for p in targets:
                with contextlib.suppress(OSError):
                    p.unlink()

    def merge(self, issue: str, slot: str, content: Mapping[str, Any]) -> dict[str, Any]:
        fields = self.get(issue, slot)
        return {**content, **fields} if fields else dict(content)

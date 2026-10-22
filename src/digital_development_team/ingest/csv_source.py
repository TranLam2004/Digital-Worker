"""CSV backlog. Header bắt buộc: title. Tuỳ chọn: id · body · labels (a;b) · priority · blocked_by · attachments.

Tên cột không phân biệt hoa thường; chấp nhận alias phổ biến (summary→title, description→body, key/number→id).
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from digital_development_team.core.errors import ConfigError
from digital_development_team.ingest.base import RawItem, normalize_priority, split_list
from digital_development_team.util.fsio import ENCODING

_ALIASES = {
    "id": ("id", "key", "number", "issue", "source_id", "ticket"),
    "title": ("title", "summary", "subject", "name"),
    "body": ("body", "description", "desc", "details", "content"),
    "labels": ("labels", "label", "tags", "type"),
    "priority": ("priority", "priority_hint", "severity", "prio"),
    "blocked_by": ("blocked_by", "depends_on", "blockedby"),
    "attachments": ("attachments", "files"),
}


def _map_columns(header: list[str]) -> dict[str, str]:
    low = {h.strip().lower(): h for h in header}
    out: dict[str, str] = {}
    for field, names in _ALIASES.items():
        for n in names:
            if n in low:
                out[field] = low[n]
                break
    if "title" not in out:
        raise ConfigError("CSV thiếu cột title", hint=f"cột có: {header}")
    return out


class CsvSource:
    name = "csv"

    def __init__(self, path: Path, *, source_name: str | None = None) -> None:
        self._path = path
        if source_name:
            self.name = source_name

    def items(self) -> Iterator[RawItem]:
        with self._path.open(encoding=ENCODING, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ConfigError(f"{self._path}: CSV rỗng")
            cols = _map_columns(list(reader.fieldnames))
            for i, row in enumerate(reader, start=2):
                title = (row.get(cols["title"]) or "").strip()
                if not title:
                    continue  # dòng trống
                sid = (row.get(cols["id"]) or "").strip() if "id" in cols else ""
                yield RawItem(
                    source=self.name,
                    source_id=sid or f"{self._path.stem}#{i - 1}",
                    title=title,
                    body=(row.get(cols["body"]) or "").strip() if "body" in cols else "",
                    labels=split_list(row.get(cols["labels"])) if "labels" in cols else (),
                    attachments=split_list(row.get(cols["attachments"])) if "attachments" in cols else (),
                    priority_hint=normalize_priority(row.get(cols["priority"]))
                    if "priority" in cols
                    else None,
                    blocked_by=split_list(row.get(cols["blocked_by"])) if "blocked_by" in cols else (),
                    raw={k: v for k, v in row.items() if k is not None},
                )

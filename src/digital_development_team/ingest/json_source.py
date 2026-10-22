"""JSON backlog: list object (hoặc {"items": [...]}). Hiểu dạng phẳng và dạng GitHub issues export.

Phẳng:  {"id": "42", "title": "...", "body": "...", "labels": ["bug"], "priority": "P1", "blocked_by": ["41"]}
GitHub: {"number": 42, "title": "...", "body": "...", "labels": [{"name": "bug"}], "html_url": "..."}
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from digital_development_team.core.errors import ConfigError
from digital_development_team.ingest.base import RawItem, normalize_priority, split_list
from digital_development_team.util import jsonio


def _labels(v: Any) -> tuple[str, ...]:
    if isinstance(v, list):
        return tuple(str(x["name"] if isinstance(x, dict) and "name" in x else x).strip() for x in v if x)
    return split_list(v)


def to_raw_item(obj: dict[str, Any], *, source: str, index: int) -> RawItem:
    sid = obj.get("id") or obj.get("number") or obj.get("key") or obj.get("source_id") or f"#{index}"
    title = str(obj.get("title") or obj.get("summary") or "").strip()
    if not title:
        raise ConfigError(f"item {sid}: thiếu title")
    return RawItem(
        source=str(obj.get("source") or source),
        source_id=str(sid),
        title=title,
        body=str(obj.get("body") or obj.get("description") or ""),
        labels=_labels(obj.get("labels")),
        attachments=split_list(obj.get("attachments")),
        priority_hint=normalize_priority(
            obj.get("priority") or obj.get("priority_hint") or obj.get("severity")
        ),
        blocked_by=split_list(obj.get("blocked_by") or obj.get("depends_on")),
        raw=obj,
    )


class JsonSource:
    name = "json"

    def __init__(self, path: Path, *, source_name: str | None = None) -> None:
        self._path = path
        if source_name:
            self.name = source_name

    def items(self) -> Iterator[RawItem]:
        data = jsonio.load_file(self._path)
        if isinstance(data, dict):
            data = data.get("items") or data.get("issues") or []
        if not isinstance(data, list):
            raise ConfigError(f"{self._path}: JSON phải là list hoặc {{items: [...]}}")
        for i, obj in enumerate(data, start=1):
            if not isinstance(obj, dict):
                raise ConfigError(f"{self._path}: phần tử {i} không phải object")
            yield to_raw_item(obj, source=self.name, index=i)

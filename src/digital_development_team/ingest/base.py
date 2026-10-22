"""BacklogSource Protocol + RawItem — mọi adapter (csv, json, github, jira) trả cùng một dạng thô."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RawItem(BaseModel):
    """Một dòng backlog đã chuẩn hóa tối thiểu. body giữ NGUYÊN VĂN (bọc <untrusted_input> khi vào brief)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    source_id: str
    title: str
    body: str = ""
    labels: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    priority_hint: str | None = None  # P0..P3
    blocked_by: tuple[str, ...] = ()  # source_id của issue chặn (cùng nguồn) hoặc DDT-xxxx
    raw: dict[str, Any] = Field(default_factory=dict)
    deadline_at: datetime | None = None  # hạn nộp (giờ máy, đã quy đổi từ giờ server)


class BacklogSource(Protocol):
    @property
    def name(self) -> str: ...

    def items(self) -> Iterator[RawItem]: ...


_PRIORITY_RE = ("P0", "P1", "P2", "P3")


def normalize_priority(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip().upper()
    if not v:
        return None
    if v in _PRIORITY_RE:
        return v
    aliases = {
        "CRITICAL": "P0",
        "BLOCKER": "P0",
        "HIGHEST": "P0",
        "HIGH": "P1",
        "MEDIUM": "P2",
        "NORMAL": "P2",
        "LOW": "P3",
        "LOWEST": "P3",
    }
    if v in aliases:
        return aliases[v]
    if v.isdigit() and 0 <= int(v) <= 3:
        return f"P{v}"
    return None


def split_list(value: Any, sep: str = ";") -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(x).strip() for x in value if str(x).strip())
    return tuple(x.strip() for x in str(value).replace(",", sep).split(sep) if x.strip())

"""Backlog từ Game Master → RawItem. Đề hiện trên trang match là nguồn; mỗi lần đề đổi là một phiên bản mới.

source_id = `BUG-01` (lần đầu) · `BUG-01@v2` (đề đổi giữa trận) — bảng queue giữ (source, source_id) duy nhất.
Hạn nộp: giờ server + độ lệch (giờ máy − giờ server) đo từ serverTime của snapshot → so được với đồng hồ máy.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from digital_development_team.gamemaster.models import BacklogItem, ItemType
from digital_development_team.ingest.base import RawItem

SOURCE = "gamemaster"
_PRIORITY = {ItemType.PI: "P0", ItemType.BUG: "P1", ItemType.CR: "P2", ItemType.TASK: "P3"}


def fingerprint(item: BacklogItem) -> str:
    return hashlib.sha256(repr(item.fingerprint()).encode("utf-8")).hexdigest()[:16]


def source_id_for(item_id: str, version: int) -> str:
    return item_id if version <= 1 else f"{item_id}@v{version}"


def item_id_of(source_id: str) -> str:
    return source_id.split("@v", 1)[0]


def to_raw_item(item: BacklogItem, *, match_id: str, offset: timedelta, version: int = 1) -> RawItem:
    """offset = giờ máy − giờ server (cộng vào mốc giờ server để so với đồng hồ máy)."""
    header = (
        f"[{item.type.value} {item.id}"
        + (f" · {item.points:g} points" if item.points is not None else "")
        + "]"
    )
    return RawItem(
        source=SOURCE,
        source_id=source_id_for(item.id, version),
        title=item.title or item.id,
        body=f"{header}\n\n{item.description}".strip(),
        labels=(item.type.value.lower(), "contest"),
        priority_hint=_PRIORITY[item.type],
        deadline_at=item.deadline_at + offset if item.deadline_at else None,
        raw={
            "match_id": match_id,
            "item_id": item.id,
            "item_type": item.type.value,
            "intake_type": item.type.intake_type,
            "points": item.points,
            "version": version,
            "fingerprint": fingerprint(item),
            "revealed_at": item.revealed_at.isoformat() if item.revealed_at else None,
        },
    )

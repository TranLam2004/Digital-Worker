"""Xếp lịch item thi đấu — tất định, 0 LLM, tính lại mỗi khi có item mới / đề đổi / slot rảnh.

Thứ tự: PI trước hết (điểm theo thứ hạng tốc độ, tính từ lúc đề hiện) → item sắp hết hạn mà còn kịp → điểm/phút
ước tính cao → hạn gần → đến trước. Item không kịp làm trước hạn (trừ biên nộp bài) bị bỏ để dành slot cho item khác.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

EST_MINUTES = {"PI": 8.0, "BUG": 10.0, "CR": 14.0, "TASK": 14.0}
"""Ước lượng phút cho một item (pipeline đầy đủ). Chỉnh sau khi đo trận giả."""
MIN_MINUTES = {"PI": 3.0, "BUG": 5.0, "CR": 7.0, "TASK": 7.0}
"""Dưới mức này trước hạn thì không bắt đầu nữa: chắc chắn không kịp, chỉ đốt credit."""


@dataclass(frozen=True, slots=True)
class Candidate:
    issue_id: str
    item_type: str  # TASK | CR | BUG | PI
    points: float | None
    deadline_at: datetime | None
    enqueued_at: str = ""


@dataclass(frozen=True, slots=True)
class Plan:
    order: tuple[str, ...]  # issue nên chạy, ưu tiên trước
    skip: tuple[tuple[str, str], ...]  # (issue, lý do) — không kịp


def _minutes_left(c: Candidate, now: datetime, margin_s: float) -> float | None:
    if c.deadline_at is None:
        return None
    return ((c.deadline_at - now).total_seconds() - margin_s) / 60.0


def plan(cands: Iterable[Candidate], *, now: datetime, margin_s: float = 90.0) -> Plan:
    keep: list[tuple[tuple[float, float, float, float, str], str]] = []
    skip: list[tuple[str, str]] = []
    for c in cands:
        t = c.item_type if c.item_type in EST_MINUTES else "TASK"
        left = _minutes_left(c, now, margin_s)
        if left is not None and left < MIN_MINUTES[t]:
            skip.append(
                (c.issue_id, f"còn {max(left, 0):.1f} phút < {MIN_MINUTES[t]:g} phút tối thiểu cho {t}")
            )
            continue
        est = EST_MINUTES[t]
        density = (c.points if c.points is not None else 100.0) / est
        urgent = left is not None and left < 2 * est  # sắp hết hạn mà còn kịp → làm ngay kẻo mất
        key = (
            0.0 if t == "PI" else 1.0,
            0.0 if urgent else 1.0,
            -density,
            left if left is not None else 1e9,
            c.enqueued_at,
        )
        keep.append((key, c.issue_id))
    keep.sort(key=lambda kv: kv[0])
    return Plan(order=tuple(i for _, i in keep), skip=tuple(skip))


def pick(order: Sequence[str], types: dict[str, str], *, free_general: int, free_pi: int) -> list[str]:
    """Chọn issue cho slot rảnh. Slot PI chỉ nhận PI; slot thường nhận mọi loại (PI cũng vào nếu slot PI đầy)."""
    out: list[str] = []
    for issue in order:
        if types.get(issue) == "PI" and free_pi > 0:
            free_pi -= 1
            out.append(issue)
        elif free_general > 0:
            free_general -= 1
            out.append(issue)
    return out

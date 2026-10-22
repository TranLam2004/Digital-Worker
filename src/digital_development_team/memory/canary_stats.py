"""Số đo canary của Verifier (bảng canary_results, ghi bởi runner/stages/canary.py). Dùng chung runner, web, báo cáo."""

from __future__ import annotations

from typing import Any

from digital_development_team.storage.db import Database

MIN_SHOWN = 3
"""Dưới số lần này chưa đủ căn cứ: không nâng tier, màn Đánh giá hiện "chưa đủ"."""


def catch_rate(db: Database) -> tuple[float | None, int]:
    """(tỷ lệ bắt, số lần) — tỷ lệ None khi chưa đủ MIN_SHOWN lần."""
    row = db.one("SELECT COUNT(*) n, COALESCE(SUM(caught), 0) c FROM canary_results")
    n = int(row["n"]) if row else 0
    if row is None or n < MIN_SHOWN:
        return None, n
    return round(int(row["c"]) / n, 3), n


def canary_summary(db: Database) -> dict[str, Any]:
    rate, n = catch_rate(db)
    rows = db.all(
        "SELECT mutation, COUNT(*) n, COALESCE(SUM(caught), 0) c FROM canary_results GROUP BY mutation ORDER BY mutation"
    )
    return {
        "shown": n,
        "caught": int(db.scalar("SELECT COALESCE(SUM(caught), 0) FROM canary_results") or 0),
        "catch_rate": rate,
        "min_shown": MIN_SHOWN,
        "by_mutation": [
            {"mutation": r["mutation"], "shown": int(r["n"]), "caught": int(r["c"])} for r in rows
        ],
    }


__all__ = ["MIN_SHOWN", "canary_summary", "catch_rate"]

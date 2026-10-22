"""Budget cả phiên (budgets.yaml#session): tổng token và USD mọi issue trong home so với trần.

Mức: ok · warn (≥ warn_at) · economy (≥ economy_at) · stop (≥ stop_new_at: không nhận issue mới).
Worker cưỡng chế mức stop và phát budget_warning khi qua mỗi mức; dashboard hiện banner theo cùng hàm.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from digital_development_team.config.models import SessionBudget

LEVELS = ("ok", "warn", "economy", "stop")


def session_status(tokens: int, usd: float, cfg: SessionBudget) -> dict[str, Any]:
    ratio = max(tokens / cfg.tokens if cfg.tokens else 0.0, usd / cfg.usd if cfg.usd else 0.0)
    if ratio >= cfg.stop_new_at:
        level = "stop"
    elif ratio >= cfg.economy_at:
        level = "economy"
    elif ratio >= cfg.warn_at:
        level = "warn"
    else:
        level = "ok"
    return {
        "tokens": int(tokens),
        "usd": round(float(usd), 4),
        "cap_tokens": cfg.tokens,
        "cap_usd": cfg.usd,
        "ratio": round(ratio, 4),
        "level": level,
        "warn_at": cfg.warn_at,
        "economy_at": cfg.economy_at,
        "stop_new_at": cfg.stop_new_at,
    }


def session_totals(usages: Iterable[Any]) -> tuple[int, float]:
    """Cộng BudgetUsage của các checkpoint (thuộc tính tokens, cost_usd)."""
    tokens, usd = 0, 0.0
    for u in usages:
        tokens += int(getattr(u, "tokens", 0) or 0)
        usd += float(getattr(u, "cost_usd", 0.0) or 0.0)
    return tokens, usd


__all__ = ["LEVELS", "session_status", "session_totals"]

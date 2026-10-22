"""USD từ Usage theo models.yaml#pricing (USD / 1M token). Chỉ để hiển thị — không phải hoá đơn."""

from __future__ import annotations

from collections.abc import Mapping

from digital_development_team.config.models import PriceCfg
from digital_development_team.core.llm import Usage

_PER = 1_000_000


def cost_usd(model: str, usage: Usage, pricing: Mapping[str, PriceCfg]) -> float:
    price = pricing.get(model)
    if price is None:
        return 0.0
    uncached = max(usage.tokens_in - usage.tokens_cached, 0)
    cached_rate = price.cached_in if price.cached_in is not None else price.in_
    cost = uncached * price.in_ + usage.tokens_cached * cached_rate + usage.tokens_out * price.out
    return round(cost / _PER, 6)

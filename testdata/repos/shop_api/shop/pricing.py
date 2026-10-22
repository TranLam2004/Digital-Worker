"""Giảm giá và thuế trên số tiền int cent. Làm tròn nửa lên (ROUND_HALF_UP) theo chuẩn kế toán."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def apply_discount(amount_cents: int, percent: int) -> int:
    """Giảm percent% (0..100). Trả int cent, làm tròn nửa lên."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be within 0..100")
    if amount_cents < 0:
        raise ValueError("amount must be >= 0")
    v = Decimal(amount_cents) * (Decimal(100 - percent) / Decimal(100))
    return int(v.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def with_tax(amount_cents: int, rate_percent: Decimal | float | int) -> int:
    """Cộng thuế rate_percent% (vd 8 hoặc Decimal('8.5')). Làm tròn nửa lên về cent."""
    if amount_cents < 0:
        raise ValueError("amount must be >= 0")
    rate = Decimal(str(rate_percent))
    if rate < 0:
        raise ValueError("rate must be >= 0")
    v = Decimal(amount_cents) * (Decimal(1) + rate / Decimal(100))
    return int(v.quantize(Decimal(1), rounding=ROUND_HALF_UP))

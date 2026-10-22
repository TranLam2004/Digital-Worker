"""Giỏ hàng. Tiền tính bằng int cent để tránh lỗi float."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CartItem:
    sku: str
    unit_price_cents: int
    qty: int


@dataclass
class Cart:
    items: dict[str, CartItem] = field(default_factory=dict)

    def add(self, sku: str, unit_price_cents: int, qty: int = 1) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        if unit_price_cents < 0:
            raise ValueError("price must be >= 0")
        cur = self.items.get(sku)
        if cur is not None:
            self.items[sku] = CartItem(sku, cur.unit_price_cents, cur.qty + qty)
        else:
            self.items[sku] = CartItem(sku, unit_price_cents, qty)

    def remove(self, sku: str) -> None:
        if sku not in self.items:
            raise KeyError(sku)
        del self.items[sku]

    def subtotal_cents(self) -> int:
        return sum(i.unit_price_cents * i.qty for i in self.items.values())

    def is_empty(self) -> bool:
        return not self.items

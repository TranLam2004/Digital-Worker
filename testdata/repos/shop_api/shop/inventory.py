"""Kho hàng. reserve() giữ hàng cho đơn, release() trả lại."""

from __future__ import annotations


class InsufficientStock(Exception):
    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(f"{sku}: requested {requested}, available {available}")
        self.sku = sku
        self.requested = requested
        self.available = available


class Inventory:
    def __init__(self) -> None:
        self._stock: dict[str, int] = {}
        self._reserved: dict[str, int] = {}

    def restock(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0) - self._reserved.get(sku, 0)

    def reserve(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._reserved[sku] = self._reserved.get(sku, 0) + qty

    def release(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        cur = self._reserved.get(sku, 0)
        if qty > cur:
            raise ValueError(f"{sku}: release {qty} > reserved {cur}")
        self._reserved[sku] = cur - qty

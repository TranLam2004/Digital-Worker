# REPO.md — shop_api

## Overview
Thư viện giỏ hàng/kho/giá mini (`shop/`): `Cart` (int cent), `Inventory` (restock/reserve/release), `pricing` (giảm giá, thuế — Decimal, ROUND_HALF_UP). Python 3.12, không dependency ngoài.

## Build & test
- test_command: `pytest -q`
- Không build.

## Layout
- `shop/cart.py` — Cart, CartItem
- `shop/inventory.py` — Inventory, InsufficientStock
- `shop/pricing.py` — apply_discount, with_tax
- `tests/` — pytest, một file test cho mỗi module

## Hot files
- shop/inventory.py
- shop/cart.py

## Conventions
- Tiền luôn là int cent; không dùng float cho tiền.
- Tham số sai → raise ValueError; thiếu hàng → raise InsufficientStock (đã khai báo trong inventory.py).
- Mỗi hàm public có test; sửa bug phải kèm regression test.

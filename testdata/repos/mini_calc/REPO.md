# REPO.md — mini_calc

## Overview
Thư viện tính toán 1 file (`calc.py`): add · mean · median. Python 3.12, không dependency.

## Build & test
- test_command: `pytest -q`
- Không build.

## Layout
- `calc.py` — toàn bộ logic
- `tests/test_calc.py` — pytest

## Hot files
- calc.py

## Conventions
- Rỗng → raise ValueError (xem median). Không trả None.

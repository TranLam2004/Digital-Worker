from decimal import Decimal

import pytest

from shop import apply_discount, with_tax


def test_apply_discount():
    assert apply_discount(1000, 10) == 900
    assert apply_discount(999, 50) == 500  # 499.5 → 500 (half up)
    assert apply_discount(1000, 0) == 1000
    assert apply_discount(1000, 100) == 0


def test_apply_discount_invalid():
    with pytest.raises(ValueError):
        apply_discount(1000, 101)


def test_with_tax():
    assert with_tax(1000, 8) == 1080
    assert with_tax(1000, Decimal("8.5")) == 1085
    assert with_tax(1, 50) == 2  # 1.5 → 2

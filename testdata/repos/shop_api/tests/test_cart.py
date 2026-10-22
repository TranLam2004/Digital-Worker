import pytest

from shop import Cart


def test_add_and_subtotal():
    c = Cart()
    c.add("A", 1000, 2)
    c.add("B", 250)
    assert c.subtotal_cents() == 2250


def test_add_same_sku_accumulates():
    c = Cart()
    c.add("A", 1000, 1)
    c.add("A", 1000, 2)
    assert c.items["A"].qty == 3


def test_remove():
    c = Cart()
    c.add("A", 1000)
    c.remove("A")
    assert c.is_empty()
    with pytest.raises(KeyError):
        c.remove("A")


def test_bad_qty():
    with pytest.raises(ValueError):
        Cart().add("A", 100, 0)

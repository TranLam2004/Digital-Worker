import pytest

from shop import Inventory


def test_restock_and_available():
    inv = Inventory()
    inv.restock("A", 5)
    assert inv.available("A") == 5
    assert inv.available("ZZZ") == 0


def test_reserve_and_release():
    inv = Inventory()
    inv.restock("A", 5)
    inv.reserve("A", 3)
    assert inv.available("A") == 2
    inv.release("A", 1)
    assert inv.available("A") == 3


def test_release_more_than_reserved():
    inv = Inventory()
    inv.restock("A", 5)
    inv.reserve("A", 1)
    with pytest.raises(ValueError):
        inv.release("A", 2)

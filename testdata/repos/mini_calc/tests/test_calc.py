import pytest

from calc import add, mean, median


def test_add():
    assert add(1, 2) == 3


def test_mean():
    assert mean([2, 4]) == 3.0


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([1, 2, 3, 4]) == 2.5


def test_median_empty():
    with pytest.raises(ValueError):
        median([])

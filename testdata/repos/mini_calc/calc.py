"""mini_calc — repo đích tối giản cho test E2E. Bug: mean([]) raise ZeroDivisionError thay vì ValueError."""


def add(a: float, b: float) -> float:
    return a + b


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2

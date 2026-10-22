"""Hàm whitelist cho biểu thức. Thêm hàm = thêm vào FUNCTIONS + test."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from digital_development_team.core.errors import ExpressionError
from digital_development_team.util.globs import GlobSet


def _as_list(x: Any) -> list[Any]:
    if x is None:
        return []
    if isinstance(x, (list, tuple, set)):
        return list(x)
    return [x]


def f_len(x: Any) -> int:
    if x is None:
        return 0
    try:
        return len(x)
    except TypeError as e:
        raise ExpressionError(f"len() không áp dụng cho {type(x).__name__}") from e


def f_any_match(paths: Any, patterns: Any) -> bool:
    """any_match(files_touched, 'migrations/**') hoặc any_match(files, ['a/**', 'b/**'])."""
    return GlobSet(_as_list(patterns)).any_match(str(p) for p in _as_list(paths))


def f_any_in(items: Any, globs_or_values: Any) -> bool:
    """any_in(files_touched, sensitive_paths) — sensitive_paths là list glob hoặc list giá trị."""
    pats = _as_list(globs_or_values)
    if pats and any(isinstance(p, str) and any(ch in p for ch in "*?[{") for p in pats):
        return f_any_match(items, pats)
    s = set(pats)
    return any(i in s for i in _as_list(items))


def f_pluck(items: Any, key: str) -> list[Any]:
    """pluck(plan.steps, 'action') → ['write_failing_test', 'modify', …]."""
    out: list[Any] = []
    for it in _as_list(items):
        if isinstance(it, dict):
            if key in it:
                out.append(it[key])
        elif hasattr(it, key):
            out.append(getattr(it, key))
    return out


def f_min(*xs: Any) -> Any:
    return min(xs)


def f_max(*xs: Any) -> Any:
    return max(xs)


def f_startswith(s: Any, prefix: str) -> bool:
    return isinstance(s, str) and s.startswith(prefix)


def f_contains(s: Any, sub: str) -> bool:
    if isinstance(s, str):
        return sub in s
    return sub in _as_list(s)


def f_any(items: Any) -> bool:
    return any(_as_list(items))


def f_all(items: Any) -> bool:
    return all(_as_list(items))


# default(x, v) và has(obj, 'k') cần đánh giá LƯỜI (bắt MissingValue) → compiler xử lý riêng.
LAZY_FUNCTIONS = frozenset({"default", "has"})

FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": f_len,
    "any_match": f_any_match,
    "any_in": f_any_in,
    "pluck": f_pluck,
    "min": f_min,
    "max": f_max,
    "startswith": f_startswith,
    "contains": f_contains,
    "any": f_any,
    "all": f_all,
}


def known_functions() -> Sequence[str]:
    return sorted({*FUNCTIONS, *LAZY_FUNCTIONS})


def iter_names(x: Iterable[Any]) -> list[str]:
    return [str(i) for i in x]

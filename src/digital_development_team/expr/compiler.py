"""Compile ast → cây closure. Đánh giá bằng cách gọi closure với scope (dict lồng nhau).

Thiếu giá trị → MissingValue. Chỉ default() và has() bắt được nó.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from digital_development_team.core.errors import ExpressionError, MissingValue
from digital_development_team.expr.functions import FUNCTIONS, LAZY_FUNCTIONS
from digital_development_team.expr.parser import parse

Scope = Mapping[str, Any]
Fn = Callable[[Scope], Any]

_CMP: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in (b if b is not None else ()),
    ast.NotIn: lambda a, b: a not in (b if b is not None else ()),
}


@dataclass(frozen=True, slots=True)
class CompiledExpr:
    source: str
    _fn: Fn
    names: frozenset[str]  # tên gốc (root) được tham chiếu — cho config check

    def __call__(self, scope: Scope) -> Any:
        return self._fn(scope)

    def as_bool(self, scope: Scope) -> bool:
        return bool(self._fn(scope))


def compile_expr(src: str) -> CompiledExpr:
    tree = parse(src)
    names: set[str] = set()
    fn = _compile(tree.body, src, names)
    return CompiledExpr(source=src, _fn=fn, names=frozenset(names))


def compile_many(srcs: list[str]) -> list[CompiledExpr]:
    return [compile_expr(s) for s in srcs]


def _lookup(obj: Any, key: str, src: str) -> Any:
    if isinstance(obj, Mapping):
        if key in obj:
            return obj[key]
        raise MissingValue(f"thiếu '{key}'", expr=src, hint=f"dùng default({key}, …) nếu optional")
    if hasattr(obj, key):
        return getattr(obj, key)
    raise MissingValue(f"'{type(obj).__name__}' không có '{key}'", expr=src)


def _compile(node: ast.expr, src: str, names: set[str]) -> Fn:
    if isinstance(node, ast.Constant):
        v = node.value
        return lambda _s: v

    if isinstance(node, ast.Name):
        name = node.id
        names.add(name)
        return lambda s: _lookup(s, name, src)

    if isinstance(node, ast.Attribute):
        base = _compile(node.value, src, names)
        attr = node.attr
        if attr == "length":  # .length → len()
            return lambda s: len(base(s) or ())
        return lambda s: _lookup(base(s), attr, src)

    if isinstance(node, ast.Subscript):
        base = _compile(node.value, src, names)
        idx = _compile(node.slice, src, names)

        def _sub(s: Scope) -> Any:
            b, i = base(s), idx(s)
            try:
                return b[i]
            except (KeyError, IndexError, TypeError) as e:
                raise MissingValue(f"index {i!r} không có", expr=src) from e

        return _sub

    if isinstance(node, (ast.List, ast.Tuple)):
        elts = [_compile(e, src, names) for e in node.elts]
        return lambda s: [e(s) for e in elts]

    if isinstance(node, ast.UnaryOp):
        operand = _compile(node.operand, src, names)
        if isinstance(node.op, ast.Not):
            return lambda s: not operand(s)
        if isinstance(node.op, ast.USub):
            return lambda s: -operand(s)
        raise ExpressionError("toán tử một ngôi không hỗ trợ", expr=src)

    if isinstance(node, ast.BoolOp):
        parts = [_compile(v, src, names) for v in node.values]
        if isinstance(node.op, ast.And):

            def _and(s: Scope) -> Any:
                r: Any = True
                for p in parts:
                    r = p(s)
                    if not r:
                        return r
                return r

            return _and

        def _or(s: Scope) -> Any:
            r: Any = False
            for p in parts:
                r = p(s)
                if r:
                    return r
            return r

        return _or

    if isinstance(node, ast.Compare):
        left = _compile(node.left, src, names)
        ops = [_CMP[type(op)] for op in node.ops]
        rights = [_compile(c, src, names) for c in node.comparators]

        def _cmp(s: Scope) -> bool:
            a = left(s)
            for op, rc in zip(ops, rights, strict=True):
                b = rc(s)
                if not op(a, b):
                    return False
                a = b
            return True

        return _cmp

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):  # parser đã chặn method call; phòng thủ thêm
            raise ExpressionError("chỉ gọi hàm theo tên")
        fname = node.func.id
        if fname in LAZY_FUNCTIONS:
            return _compile_lazy(fname, node, src, names)
        if fname not in FUNCTIONS:
            raise ExpressionError(
                f"hàm không cho phép: {fname}()", expr=src, hint=f"cho phép: {sorted(FUNCTIONS)}"
            )
        f = FUNCTIONS[fname]
        args = [_compile(a, src, names) for a in node.args]
        return lambda s: f(*(a(s) for a in args))

    raise ExpressionError(f"node không hỗ trợ: {type(node).__name__}", expr=src)


def _compile_lazy(fname: str, node: ast.Call, src: str, names: set[str]) -> Fn:
    if fname == "default":
        if len(node.args) != 2:
            raise ExpressionError("default(x, fallback) cần đúng 2 tham số", expr=src)
        x = _compile(node.args[0], src, names)
        d = _compile(node.args[1], src, names)

        def _default(s: Scope) -> Any:
            try:
                v = x(s)
            except MissingValue:
                return d(s)
            return d(s) if v is None else v

        return _default

    if fname == "has":
        if (
            len(node.args) != 2
            or not isinstance(node.args[1], ast.Constant)
            or not isinstance(node.args[1].value, str)
        ):
            raise ExpressionError("has(obj, 'key') cần key là chuỗi hằng", expr=src)
        obj = _compile(node.args[0], src, names)
        key = node.args[1].value

        def _has(s: Scope) -> bool:
            try:
                o = obj(s)
            except MissingValue:
                return False
            if isinstance(o, Mapping):
                return key in o
            return hasattr(o, key)

        return _has

    raise ExpressionError(f"lazy fn lạ: {fname}", expr=src)  # pragma: no cover

"""Chuẩn hóa dialect → parse ast → kiểm whitelist node."""

from __future__ import annotations

import ast

from digital_development_team.core.errors import ExpressionError

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Call,
)

_FORBIDDEN_NAMES = {
    "__import__",
    "eval",
    "exec",
    "open",
    "globals",
    "locals",
    "getattr",
    "setattr",
    "delattr",
    "compile",
}


def normalize_dialect(src: str) -> str:
    """&& → and · || → or · !x → not x (giữ !=) · true/false/null → True/False/None. Bỏ qua trong string literal."""
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            continue
        if src.startswith("&&", i):
            out.append(" and ")
            i += 2
            continue
        if src.startswith("||", i):
            out.append(" or ")
            i += 2
            continue
        if c == "!" and not src.startswith("!=", i):
            out.append(" not ")
            i += 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            out.append({"true": "True", "false": "False", "null": "None"}.get(word, word))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse(src: str) -> ast.Expression:
    normalized = normalize_dialect(src).strip()
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"cú pháp sai: {e.msg}", expr=src, normalized=normalized, col=e.offset) from e
    _check(tree, src)
    return tree


def _check(tree: ast.Expression, src: str) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"node không cho phép: {type(node).__name__}", expr=src)
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in _FORBIDDEN_NAMES):
            raise ExpressionError(f"tên bị cấm: {node.id}", expr=src)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExpressionError(f"thuộc tính bị cấm: .{node.attr}", expr=src)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError("chỉ được gọi hàm theo tên (không gọi method)", expr=src)
            if node.keywords:
                raise ExpressionError("không hỗ trợ keyword argument", expr=src)
        if isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, float, bool, type(None))):
            raise ExpressionError(f"hằng không cho phép: {node.value!r}", expr=src)

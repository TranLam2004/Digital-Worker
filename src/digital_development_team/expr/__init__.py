"""Biểu thức an toàn cho rule trong config. Không eval — xem README › Quyết định kiến trúc.

    expr = compile_expr("intake.confidence < 0.6 and not default(state.touches_sensitive, false)")
    expr(scope)  # scope: dict lồng nhau

- Không eval. ast whitelist. Compile một lần lúc nạp config.
- Thiếu giá trị → MissingValue (fail closed). Dùng default(x, v) hoặc has(obj, 'key') cho optional.
- Dialect: chấp nhận && || ! true false null và .length; chuẩn hóa về Python.
"""

from __future__ import annotations

from digital_development_team.expr.compiler import CompiledExpr, compile_expr, compile_many
from digital_development_team.expr.functions import FUNCTIONS

__all__ = ["FUNCTIONS", "CompiledExpr", "compile_expr", "compile_many"]

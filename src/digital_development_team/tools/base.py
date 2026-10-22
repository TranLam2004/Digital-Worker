"""ToolRegistry — canonical name → impl (Tool Protocol). Tool KHÔNG tự check policy; Dispatcher làm."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from digital_development_team.core.context import ToolContext
from digital_development_team.core.models import ToolResult
from digital_development_team.tools.catalog import ToolCatalog, ToolOp

Handler = Callable[[Mapping[str, Any], ToolContext, str | None], ToolResult]
"""(args, ctx, profile_suffix) → ToolResult. profile_suffix: 'readonly' | 'scan' | None (từ exec.run:readonly)."""


class ToolRegistry:
    def __init__(self, catalog: ToolCatalog) -> None:
        self._catalog = catalog
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, handler: Handler) -> None:
        self._catalog.get(name)  # phải có trong catalog
        self._handlers[name] = handler

    def register_namespace(self, namespace: str, handlers: Mapping[str, Handler]) -> None:
        for op, h in handlers.items():
            self.register(f"{namespace}.{op}", h)

    def has(self, name: str) -> bool:
        return name.split(":", 1)[0] in self._handlers

    def handler(self, name: str) -> Handler | None:
        return self._handlers.get(name.split(":", 1)[0])

    def op(self, name: str) -> ToolOp:
        return self._catalog.get(name)

    def names(self) -> list[str]:
        return sorted(self._handlers)

    def missing_impls(self) -> list[str]:
        """Op catalog đánh dấu implemented=true nhưng chưa đăng ký impl — contract test."""
        return [
            n for n in self._catalog.names() if self._catalog.get(n).implemented and n not in self._handlers
        ]


def not_implemented(name: str, hint: str) -> Handler:
    def _h(args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        return ToolResult.fail("NOT_IMPLEMENTED", f"{name} chưa có trong bản này", hint=hint)

    return _h

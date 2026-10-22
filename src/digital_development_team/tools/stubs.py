"""Stub cho tool chưa nối trong runtime này (codegraph khi không có repo · memory khi không có DB · user.ask · agent.*). Trả NOT_IMPLEMENTED có hint.

memory.search/get: trả rỗng status ok (agent tiếp tục không cần memory).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_development_team.core.context import ToolContext
from digital_development_team.core.models import ToolResult
from digital_development_team.tools.base import Handler, ToolRegistry, not_implemented
from digital_development_team.tools.catalog import ToolCatalog

_HINTS = {
    "codegraph": "codegraph.* cần repo git (--repo). Dùng fs.search / fs.list skeleton.",
    "memory": "memory.* chưa nối (runtime không có DB). Tiếp tục không cần kinh nghiệm cũ.",
    "user": "user.ask chưa pause được (P2). Ghi escalation.yaml qua blackboard.write rồi trả blocked.",
    "agent": "Delegation chưa bật (P3). Tự làm tuần tự.",
    "llm": "llm.ask cần models.yaml#roles.llm_ask. Dùng kiến thức sẵn có.",
    "web": "web.* tắt (policy.network.web_enabled=false). Dùng fs.search / kiến thức sẵn có.",
    "docs": "docs.* tắt (policy.network.web_enabled=false). Đọc mã nguồn thư viện trong repo nếu có.",
    "skill": "skill.* chưa nối. Dùng skill có sẵn trong brief.",
}


def _memory_empty(args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
    return ToolResult.ok("memory → 0 kết quả (memory chưa bật)", results=[], count=0)


def register_stubs(registry: ToolRegistry, catalog: ToolCatalog) -> None:
    for name in catalog.names():
        if registry.has(name):
            continue
        ns = name.split(".", 1)[0]
        handler: Handler
        if name in ("memory.search", "memory.get"):
            handler = _memory_empty
        else:
            handler = not_implemented(name, _HINTS.get(ns, "chưa có"))
        registry.register(name, handler)

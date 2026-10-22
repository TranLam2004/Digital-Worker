"""skill.search · skill.load — agent tự tìm và nạp skill khi trigger cố định không chọn đúng skill cho task lạ.

Tools (layer 5) không import agents (layer 6): app tiêm một SkillLookup (agents/skills.py: SkillIndex).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from digital_development_team.core.context import ToolContext
from digital_development_team.core.models import ToolResult
from digital_development_team.tools.base import Handler


class SkillLookup(Protocol):
    def search(self, query: str, *, role: str, k: int) -> list[dict[str, Any]]: ...

    def load(self, name: str, *, role: str) -> dict[str, Any] | None: ...


class SkillTools:
    def __init__(self, lookup: SkillLookup) -> None:
        self._lookup = lookup

    def handlers(self) -> dict[str, Handler]:
        return {"search": self.search, "load": self.load}

    def search(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        query = str(args.get("query") or "").strip()
        k = max(1, min(int(args.get("k") or 5), 8))
        hits = self._lookup.search(query, role=ctx.actor.role, k=k)
        return ToolResult.ok(f"skill.search '{query[:40]}' → {len(hits)}", results=hits, count=len(hits))

    def load(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        name = str(args.get("name") or "").strip()
        skill = self._lookup.load(name, role=ctx.actor.role)
        if skill is None:
            return ToolResult.fail("NOT_FOUND", f"skill {name!r} not found", hint="Use skill.search first")
        return ToolResult.ok(f"skill {name} ({skill.get('tokens')} tokens)", **skill)

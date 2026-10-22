"""Memory — episodic (episodes + FTS5) · semantic (facts.json) · procedural (skill học được) (50-knowledge).

KnowledgeMemory (memory/knowledge.py) dựng khối RELEVANT EXPERIENCE + skill học được cho brief; NullMemory khi --no-learn.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

NO_MEMORY_ROLES = frozenset({"verifier", "security"})
"""Verifier/Security KHÔNG nhận memory block — tránh thiên kiến 'module này hay pass' (50-knowledge/06)."""


class MemoryProvider(Protocol):
    def block(
        self, role: str, issue_id: str, facts: Mapping[str, Any], *, max_tokens: int
    ) -> tuple[str, tuple[str, ...]]:
        """(khối text '## RELEVANT EXPERIENCE' hoặc '', refs đã nạp để harness chấm điểm khi issue xong)."""
        ...

    def learned_skills(
        self, role: str, issue_id: str, facts: Mapping[str, Any]
    ) -> list[tuple[str, str, int]]:
        """Skill học được cho brief: (text đã có tiêu đề + nhãn, ref `skill:<name>@<v>`, token)."""
        ...


class NullMemory:
    def block(
        self, role: str, issue_id: str, facts: Mapping[str, Any], *, max_tokens: int
    ) -> tuple[str, tuple[str, ...]]:
        return "", ()

    def learned_skills(
        self, role: str, issue_id: str, facts: Mapping[str, Any]
    ) -> list[tuple[str, str, int]]:
        return []


__all__ = ["NO_MEMORY_ROLES", "MemoryProvider", "NullMemory"]

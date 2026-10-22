"""KnowledgeMemory — MemoryProvider thật cho brief (50-knowledge/06-retrieval), 0 LLM.

Khối RELEVANT EXPERIENCE: fact đúng module (active, rồi candidate có nhãn) → lesson của episode cùng module (cùng loại
trước, người từng sửa trước) → module chưa có episode thì tìm FTS theo tiêu đề issue. Skill học được đi riêng vào phần
SKILL của brief (active trước, candidate có nhãn). Mọi thứ đã nạp trả về dạng ref để harness chấm khi issue xong.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from digital_development_team.config.models import MemoryCfg
from digital_development_team.core.errors import DDTError
from digital_development_team.memory import NO_MEMORY_ROLES
from digital_development_team.memory.episodes import EpisodeStore
from digital_development_team.memory.facts import FactStore, fact_lines
from digital_development_team.memory.learned import LearnedSkills, brief_text
from digital_development_team.memory.skill_scan import split_skill
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.text import estimate_tokens

HEADER = "## RELEVANT EXPERIENCE (from past issues in this repo — the spec wins on conflict)"


def _intake(facts: Mapping[str, Any]) -> Mapping[str, Any]:
    v = facts.get("intake")
    return v if isinstance(v, Mapping) else {}


def _title(facts: Mapping[str, Any]) -> str:
    item = facts.get("item")
    return str(item.get("title") or "") if isinstance(item, Mapping) else ""


def _lesson_line(ep: Mapping[str, Any]) -> str:
    how = "pass" if ep.get("outcome") == "pass" else str(ep.get("outcome"))
    if ep.get("rework_count"):
        how += f" after {ep['rework_count']} rework"
    if ep.get("human_correction"):
        how += ", human corrected"
    return f"- {ep['issue_id']} ({ep.get('issue_type')}, {how}): {str(ep.get('lesson') or '')[:300]}"


class KnowledgeMemory:
    def __init__(self, layout: HomeLayout, db: Database, cfg: MemoryCfg) -> None:
        self._facts = FactStore(layout)
        self._episodes = EpisodeStore(db)
        self._skills = LearnedSkills(db, layout, cfg)
        self._cfg = cfg

    def block(
        self, role: str, issue_id: str, facts: Mapping[str, Any], *, max_tokens: int
    ) -> tuple[str, tuple[str, ...]]:
        if role in NO_MEMORY_ROLES:
            return "", ()
        intake = _intake(facts)
        module = str(intake.get("affected_area") or "")
        itype = str(intake.get("type") or "")
        candidates: list[tuple[str, str]] = list(
            fact_lines(self._facts, module, candidates=self._cfg.candidates_per_brief)
        )
        try:
            eps = (
                self._episodes.related(module, itype, exclude=issue_id, k=self._cfg.episodes_per_brief)
                if module
                else []
            )
            if not eps and _title(facts):
                eps = [
                    e
                    for e in self._episodes.search(_title(facts), k=self._cfg.episodes_per_brief * 2)
                    if e.get("issue_id") != issue_id and e.get("lesson")
                ][: self._cfg.episodes_per_brief]
        except (DDTError, sqlite3.Error):
            eps = []
        candidates += [(_lesson_line(e), f"episode:{e['issue_id']}") for e in eps]
        lines = [HEADER]
        refs: list[str] = []
        for line, ref in candidates:
            if estimate_tokens("\n".join([*lines, line])) > max_tokens:
                break
            lines.append(line)
            refs.append(ref)
        return ("\n".join(lines), tuple(refs)) if refs else ("", ())

    def learned_skills(
        self, role: str, issue_id: str, facts: Mapping[str, Any]
    ) -> list[tuple[str, str, int]]:
        intake = _intake(facts)
        out: list[tuple[str, str, int]] = []
        try:
            chosen = self._skills.for_brief(
                role,
                module=str(intake.get("affected_area") or "*"),
                issue_type=str(intake.get("type") or "*"),
            )
            for s in chosen:
                parts = split_skill(self._skills.text(s.name))
                if parts is None:
                    continue
                text, tokens = brief_text(s, parts[1])
                out.append((text, s.ref, tokens))
        except (DDTError, sqlite3.Error):
            return out
        return out


__all__ = ["KnowledgeMemory"]

"""memory.* — tìm kinh nghiệm cũ (mọi role có memory) và ghi kiến thức mới (chỉ Learner).

Ghi luôn đi qua quét (memory/skill_scan.py) và vòng đời cách ly: lesson/fact/skill có injection hoặc secret bị từ chối;
fact Learner cần bằng chứng là issue đã pass; skill mới vào quarantine/candidate, harness tự nâng khi đủ lần pass.
Learner chỉ ghi lesson cho chính issue đang học (ctx.issue_id) và chỉ hạ fact do Learner tạo.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from digital_development_team.config.models import CommandGuardCfg, MemoryCfg
from digital_development_team.core.context import ToolContext
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import ToolResult
from digital_development_team.memory.episodes import EpisodeStore
from digital_development_team.memory.facts import FactStore
from digital_development_team.memory.learned import LearnedSkills
from digital_development_team.memory.skill_scan import (
    MAX_FACT_CHARS,
    MAX_LESSON_CHARS,
    ScanContext,
    scan_text,
)
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.tools.base import Handler

_WORD = re.compile(r"[a-z0-9_]{3,}")
_FACT_ID = re.compile(r"^F\d{4,}$")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _obj(args: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    v = args.get(key)
    return v if isinstance(v, Mapping) else {}


@dataclass(frozen=True)
class MemoryWiring:
    """Những gì memory.* cần từ runtime (app.build_tooling truyền vào)."""

    db: Database
    layout: HomeLayout
    cfg: MemoryCfg
    command_guard: CommandGuardCfg


class MemoryTools:
    def __init__(self, db: Database, layout: HomeLayout, cfg: MemoryCfg, scan: ScanContext) -> None:
        self._db = db
        self._episodes = EpisodeStore(db)
        self._facts = FactStore(layout)
        self._skills = LearnedSkills(db, layout, cfg)
        self._cfg = cfg
        self._scan = scan

    def handlers(self) -> dict[str, Handler]:
        return {
            "search": self.search,
            "get": self.get,
            "worker_stats": self.worker_stats,
            "write_episode": self.write_episode,
            "promote_fact": self.promote_fact,
            "demote_fact": self.demote_fact,
            "write_skill": self.write_skill,
        }

    # ───── đọc ─────
    def search(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        query = str(args.get("query") or "").strip()
        module = str(args.get("module") or "") or None
        itype = str(args.get("type") or "") or None
        words = _words(query)
        try:
            eps = self._episodes.search(query, module=module, issue_type=itype, k=5)
        except sqlite3.Error as e:
            return ToolResult.fail("MEMORY_ERROR", f"episode search failed: {e}")
        results: list[dict[str, Any]] = [
            {
                "kind": "episode",
                "id": e["issue_id"],
                "type": e["issue_type"],
                "module": e["module"],
                "outcome": e["outcome"],
                "rework": e["rework_count"],
                "lesson": e.get("lesson"),
                "failure_reason": (e.get("failure_reason") or "")[:300] or None,
                "approach": (e.get("approach") or "")[:200],
                "files": e["files"][:5],
            }
            for e in eps
            if e["issue_id"] != ctx.issue_id
        ]
        for f in self._facts.list_facts(module=module):
            if f.get("status") == "retired" or not (words & _words(str(f.get("text")))):
                continue
            results.append(
                {
                    "kind": "fact",
                    "id": f["id"],
                    "text": f["text"],
                    "module": f.get("module"),
                    "status": f["status"],
                }
            )
        for s in self._skills.list_skills():
            if s.status not in ("candidate", "active") or (module and s.module not in (module, "*")):
                continue
            if words & _words(f"{s.name} {s.description}"):
                results.append(
                    {
                        "kind": "skill",
                        "id": s.name,
                        "description": s.description,
                        "status": s.status,
                        "module": s.module,
                    }
                )
        return ToolResult.ok(
            f"memory.search '{query[:40]}' → {len(results)}", results=results, count=len(results)
        )

    def get(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        key = str(args.get("id") or "").strip()
        kind = str(args.get("kind") or "")
        if kind == "skill" or key.startswith("learned-"):
            s = self._skills.get(key)
            if s is None:
                return ToolResult.fail("NOT_FOUND", f"no learned skill {key!r}")
            return ToolResult.ok(
                f"skill {s.name} v{s.version} ({s.status})", **s.as_dict(), text=self._skills.text(s.name)
            )
        if kind == "fact" or _FACT_ID.match(key):
            f = self._facts.get(key)
            if f is None:
                return ToolResult.fail("NOT_FOUND", f"no fact {key!r}")
            return ToolResult.ok(
                f"fact {key} ({f.get('status')})", **{k: v for k, v in f.items() if k != "history"}
            )
        ep = self._episodes.get(key)
        if ep is None:
            return ToolResult.fail(
                "NOT_FOUND", f"nothing with id {key!r}", hint="ids: DDT-0001 · F0001 · learned-<slug>"
            )
        return ToolResult.ok(f"episode {key} ({ep['outcome']})", **{k: v for k, v in ep.items() if k != "id"})

    def worker_stats(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        where, params = [], []
        for col in ("role", "module"):
            if args.get(col):
                where.append(f"{col}=?")
                params.append(str(args[col]))
        sql = "SELECT role, issue_type, module, attempts, passes FROM worker_stats"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = [
            {**dict(r), "pass_rate": round(r["passes"] / r["attempts"], 3) if r["attempts"] else None}
            for r in self._db.all(sql + " ORDER BY attempts DESC LIMIT 30", params)
        ]
        return ToolResult.ok(f"worker_stats → {len(rows)}", results=rows, count=len(rows))

    # ───── ghi (Learner) ─────
    def write_episode(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        ep = _obj(args, "episode")
        lesson = " ".join(str(ep.get("lesson") or "").split())
        reason = " ".join(str(ep.get("failure_reason") or "").split())[:500] or None
        if len(lesson) < 15:
            return ToolResult.fail("VALIDATION", "lesson must be one concrete sentence (≥ 15 characters)")
        problems = scan_text(lesson, max_chars=MAX_LESSON_CHARS) + (
            scan_text(reason, max_chars=500) if reason else []
        )
        if problems:
            return ToolResult.fail(
                "VALIDATION", "; ".join(problems), hint="Rewrite as a plain factual sentence"
            )
        if not self._episodes.set_lesson(ctx.issue_id, lesson, failure_reason=reason):
            return ToolResult.fail("NOT_FOUND", f"no episode for {ctx.issue_id} yet")
        return ToolResult.ok(f"lesson saved for {ctx.issue_id}", issue_id=ctx.issue_id)

    def promote_fact(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        fact = _obj(args, "fact")
        text = " ".join(str(fact.get("text") or "").split())
        module = str(fact.get("module") or "").strip()
        evidence = [str(x) for x in fact.get("evidence") or [] if isinstance(x, str)]
        problems = scan_text(text, max_chars=MAX_FACT_CHARS)
        if len(text) < 15:
            problems.append("a fact is one concrete sentence (≥ 15 characters)")
        if not module or module == "*":
            problems.append("bind the fact to a module (intake.affected_area)")
        passed = [
            i for i in dict.fromkeys(evidence) if (self._episodes.get(i) or {}).get("outcome") == "pass"
        ]
        if len(passed) < self._cfg.promote_min_evidence:
            problems.append(
                f"evidence needs ≥ {self._cfg.promote_min_evidence} issues that passed (got {len(passed)}: "
                f"{', '.join(passed) or 'none'}); use memory.search to find them, or write a lesson instead"
            )
        if problems:
            return ToolResult.fail("VALIDATION", "; ".join(problems))
        f = self._facts.propose(
            text, module=module, evidence=passed, by="learner", initial_conf=self._cfg.promote_initial_conf
        )
        return ToolResult.ok(
            f"fact {f['id']} saved as {f['status']} (used in briefs as unverified until proven)", id=f["id"]
        )

    def demote_fact(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        fid = str(args.get("id") or "")
        reason = str(args.get("reason") or "").strip()
        f = self._facts.get(fid)
        if f is None:
            return ToolResult.fail("NOT_FOUND", f"no fact {fid!r}")
        if f.get("source") != "learner":
            return ToolResult.denied(
                "memory:human_fact",
                "facts added by people can only be changed by people",
                hint="Write a lesson",
            )
        if len(reason) < 10:
            return ToolResult.fail("VALIDATION", "give the reason (which issue showed it is wrong)")
        self._facts.apply(
            {"action": "demote", "id": fid, "reason": f"{ctx.issue_id}: {reason}"}, by="learner"
        )
        return ToolResult.ok(f"fact {fid} retired")

    def write_skill(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        sk = _obj(args, "skill")
        try:
            if sk.get("find") is not None:
                s = self._skills.patch(
                    str(sk.get("name") or ""),
                    str(sk["find"]),
                    str(sk.get("replace") or ""),
                    by="learner",
                    issue=ctx.issue_id,
                    scan=self._scan,
                )
            else:
                s = self._skills.propose(
                    str(sk.get("text") or ""), by="learner", issue=ctx.issue_id, scan=self._scan
                )
        except StoreError as e:
            return ToolResult.fail("VALIDATION", e.message, hint=e.hint)
        if s.status == "quarantine":
            return ToolResult.ok(
                f"{s.name} v{s.version} quarantined — fix and write again: {'; '.join(s.problems)[:300]}",
                name=s.name,
                version=s.version,
                status=s.status,
                problems=list(s.problems),
            )
        return ToolResult.ok(
            f"{s.name} v{s.version} is a candidate; it becomes active after it helps enough issues pass",
            name=s.name,
            version=s.version,
            status=s.status,
        )


__all__ = ["MemoryTools", "MemoryWiring"]

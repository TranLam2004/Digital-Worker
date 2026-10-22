"""SessionReport — đếm từ dữ liệu đã có: board (trạng thái), checkpoint (tier, rework, usage), events (gate, tool, llm)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from digital_development_team.blackboard.checkpoints import CheckpointStore
from digital_development_team.core.enums import EventType
from digital_development_team.core.events import Event
from digital_development_team.eventlog.reader import read_events
from digital_development_team.reporting.stats import RoleStat, derive_role_stats, render_roles
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout


@dataclass
class SessionReport:
    issues: Counter[str] = field(default_factory=Counter)  # status → n
    by_type: Counter[str] = field(default_factory=Counter)
    by_tier: Counter[str] = field(default_factory=Counter)
    gates: Counter[str] = field(default_factory=Counter)  # G0/G1 resolved · timeout
    human_wait_sec: list[int] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_denied: int = 0
    rework_total: int = 0
    rule_only_merges: int = 0
    blockers: Counter[str] = field(default_factory=Counter)
    per_issue: list[str] = field(default_factory=list)
    roles: dict[str, RoleStat] = field(default_factory=dict)


def build_report(layout: HomeLayout, db: Database) -> SessionReport:
    rep = SessionReport()
    cps = CheckpointStore(layout, db)
    events_by_issue: dict[str, list[Event]] = {}
    for row in cps.board():
        rep.issues[row.status.value] += 1
        cp = cps.load(row.issue_id)
        if cp is None:
            continue
        rep.by_tier[cp.tier.value] += 1
        rep.rework_total += cp.rework
        rep.tokens += cp.usage.tokens
        rep.cost_usd += cp.usage.cost_usd
        if cp.facts.get("gate_rule_only"):
            rep.rule_only_merges += 1
        itype = (
            (cp.facts.get("intake") or {}).get("type") if isinstance(cp.facts.get("intake"), dict) else None
        )
        if itype:
            rep.by_type[str(itype)] += 1
        evs = read_events(layout, row.issue_id)
        events_by_issue[row.issue_id] = evs
        for e in evs:
            if e.event is EventType.LLM_CALL:
                rep.llm_calls += 1
            elif e.event is EventType.TOOL_CALL:
                rep.tool_calls += 1
            elif e.event is EventType.TOOL_DENIED:
                rep.tool_denied += 1
            elif e.event in (EventType.GATE_RESOLVED, EventType.GATE_TIMEOUT) and e.human:
                rep.gates[
                    f"{e.human.gate or '?'}:{'timeout' if e.event is EventType.GATE_TIMEOUT else e.human.choice}"
                ] += 1
                if e.human.wait_sec is not None:
                    rep.human_wait_sec.append(e.human.wait_sec)
            elif e.event is EventType.BLOCKED:
                rep.blockers[str(e.data.get("reason", "?"))[:60]] += 1
        rep.per_issue.append(
            f"{row.issue_id} {itype or '?'} — {row.status.value} @{row.stage} {cp.tier.value}, rework {cp.rework}, {cp.usage.tokens} tok"
        )
    rep.roles = derive_role_stats(events_by_issue)
    return rep


def _fmt_counter(c: Counter[str]) -> str:
    return " · ".join(f"{k} {v}" for k, v in sorted(c.items())) or "—"


def render_report(rep: SessionReport, *, title: str = "DDT SESSION REPORT") -> str:
    total = sum(rep.issues.values())
    lines = [
        title,
        "",
        f"ISSUES        total {total} · {_fmt_counter(rep.issues)}",
        f"BY TYPE       {_fmt_counter(rep.by_type)}",
        f"BY TIER       {_fmt_counter(rep.by_tier)} · rule-only merge {rep.rule_only_merges}",
        f"HUMAN         gates {_fmt_counter(rep.gates)}"
        + (
            f" · avg wait {sum(rep.human_wait_sec) / len(rep.human_wait_sec):.0f}s"
            if rep.human_wait_sec
            else ""
        ),
        f"QUALITY       rework total {rep.rework_total} · tool_denied {rep.tool_denied}/{rep.tool_calls + rep.tool_denied} tool calls",
        f"COST          {rep.tokens} token · ${rep.cost_usd:.2f} · {rep.llm_calls} llm calls"
        + (f" · avg {rep.tokens // total}/issue" if total else ""),
        f"TOP BLOCKERS  {_fmt_counter(rep.blockers)}",
        *(["ROLES", render_roles(rep.roles)] if rep.roles and render_roles(rep.roles) else []),
        "",
        *rep.per_issue,
    ]
    return "\n".join(lines) + "\n"

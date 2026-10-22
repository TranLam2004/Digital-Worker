"""Thống kê theo role suy từ events (50-knowledge/05-profiles) — dùng khi worker_stats (P3) còn rỗng.

Dùng chung: `ddt report`, web /api/stats, panel ③. 0 LLM. Định nghĩa "pass" theo role:
  dev       issue_done → pass · rework → fail
  verifier  G1 approve → pass · G1 reject → fail (người không reject = Verifier đúng)
  leader    như verifier (người không override)
  planner   issue_done → pass · re_localize → fail
Các role còn lại chưa đo được từ events (cần evidence/unclear, canary) → chỉ có token/cost/tool.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from digital_development_team.core.enums import EventType
from digital_development_team.core.events import Event

ROLES: tuple[str, ...] = ("intake", "planner", "analyst", "dev", "verifier", "security", "leader", "learner")
STAGE_ROLE: dict[str, str] = {
    "TRIAGE": "intake",
    "LOCALIZE": "planner",
    "SPEC": "analyst",
    "IMPLEMENT": "dev",
    "VERIFY": "verifier",
    "REVIEW": "security",
    "GATE": "leader",
}


@dataclass
class RoleStat:
    role: str
    tasks: int = 0
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    tool_calls: int = 0
    tool_denied: int = 0
    passes: int = 0
    fails: int = 0
    issues: set[str] = field(default_factory=set)
    tools: dict[str, int] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def pass_rate(self) -> float | None:
        t = self.passes + self.fails
        return self.passes / t if t else None

    @property
    def cache_hit(self) -> float | None:
        return self.tokens_cached / self.tokens_in if self.tokens_in else None

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "tasks": self.tasks,
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cached": self.tokens_cached,
            "tokens": self.tokens,
            "tok_per_task": round(self.tokens / self.tasks) if self.tasks else None,
            "cost_usd": round(self.cost_usd, 4),
            "latency_ms_avg": round(self.latency_ms / self.calls) if self.calls else None,
            "tool_calls": self.tool_calls,
            "tool_denied": self.tool_denied,
            "passes": self.passes,
            "fails": self.fails,
            "pass_rate": self.pass_rate,
            "cache_hit": self.cache_hit,
            "issues": sorted(self.issues),
            "tools": dict(sorted(self.tools.items(), key=lambda kv: -kv[1])),
        }


def _role_of_actor(actor: str) -> str:
    return actor.split("/", 1)[0].split(":", 1)[0]


def derive_role_stats(events_by_issue: Mapping[str, Sequence[Event]]) -> dict[str, RoleStat]:
    st = {r: RoleStat(r) for r in ROLES}
    for issue, evs in events_by_issue.items():
        for e in evs:
            stage_role = STAGE_ROLE.get(e.stage.value) if e.stage else None
            actor_role = _role_of_actor(e.actor)
            r = st.get(actor_role) or (st.get(stage_role) if stage_role else None)
            if e.event is EventType.STAGE_ENTER and stage_role:
                st[stage_role].tasks += 1
                st[stage_role].issues.add(issue)
            # kết quả toàn cục do runner emit — không phụ thuộc actor
            if e.event is EventType.REWORK:
                st["dev"].fails += 1
                continue
            if e.event is EventType.RE_LOCALIZE:
                st["planner"].fails += 1
                continue
            if e.event is EventType.ISSUE_DONE:
                st["dev"].passes += 1
                st["planner"].passes += 1
                continue
            if e.event is EventType.GATE_RESOLVED and e.human and e.human.gate == "G1":
                if e.human.choice == "approve":
                    st["verifier"].passes += 1
                    st["leader"].passes += 1
                elif str(e.human.choice).startswith("reject"):
                    st["verifier"].fails += 1
                    st["leader"].fails += 1
                continue
            if r is None:
                continue
            if e.event is EventType.LLM_CALL and e.llm:
                r.calls += 1
                r.tokens_in += e.llm.tokens_in
                r.tokens_out += e.llm.tokens_out
                r.tokens_cached += e.llm.tokens_cached
                r.cost_usd += e.llm.cost_usd
                r.latency_ms += e.llm.latency_ms
                r.issues.add(issue)
            elif e.event is EventType.TOOL_CALL and e.tool:
                r.tool_calls += 1
                r.tools[e.tool.name] = r.tools.get(e.tool.name, 0) + 1
            elif e.event is EventType.TOOL_DENIED and e.tool:
                r.tool_denied += 1
                r.tools[e.tool.name] = r.tools.get(e.tool.name, 0) + 1
    return st


def role_stats_json(stats: Mapping[str, RoleStat]) -> dict[str, Any]:
    tot_in = sum(s.tokens_in for s in stats.values())
    tot_cached = sum(s.tokens_cached for s in stats.values())
    return {
        "roles": {k: v.to_json() for k, v in stats.items()},
        "totals": {
            "calls": sum(s.calls for s in stats.values()),
            "tokens": sum(s.tokens for s in stats.values()),
            "cost_usd": round(sum(s.cost_usd for s in stats.values()), 4),
            "tool_calls": sum(s.tool_calls for s in stats.values()),
            "tool_denied": sum(s.tool_denied for s in stats.values()),
            "cache_hit": (tot_cached / tot_in) if tot_in else None,
        },
    }


def render_roles(stats: Mapping[str, RoleStat]) -> str:
    """Một dòng/role cho `ddt report`."""
    rows = []
    for k in ROLES:
        s = stats[k]
        if not (s.calls or s.tasks):
            continue
        pr = s.pass_rate
        rows.append(
            f"  {k:<9} task {s.tasks:<3} pass {f'{pr:.0%}' if pr is not None else '—':<5} "
            f"tok {s.tokens:<8} cache {f'{s.cache_hit:.0%}' if s.cache_hit is not None else '—':<5} "
            f"denied {s.tool_denied}"
        )
    return "\n".join(rows)

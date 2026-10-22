"""Kiểm skill × agent lúc `ddt config check` (L6 — cần AgentRegistry + SkillRegistry, nên không nằm ở config/).

error: used_by trỏ role lạ · agent khai skill không tồn tại · agent không nằm trong used_by của skill nó khai ·
       source lạ · Σ token skill `always` của một role vượt trần brief · body skill khớp pattern injection (policy.hooks).
warn:  token > cap (chưa tới 1.5× nên registry vẫn nạp) · thiếu mục chuẩn · layout <dir>/SKILL.md mà tên thư mục ≠ name ·
       imported thiếu imported_at/adapted/dòng Nguồn.
info:  mỗi role — always / worst case / trần.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from digital_development_team.agents.skills import (
    IMPORTED,
    MAX_BRIEF_SKILL_TOKENS,
    MAX_TOKENS,
    SKILL_FILE,
    SkillRegistry,
    SkillSpec,
)
from digital_development_team.agents.spec import AgentRegistry, AgentSpec
from digital_development_team.config.checks import CheckReport
from digital_development_team.policy.hooks import injection_scan

REQUIRED_SECTIONS = ("## When to apply", "## Procedure", "## Known pitfalls", "## Output checklist")
SOURCE_LINE = "Source:"
_TYPE_RE = re.compile(r"intake\.type\s*==\s*'([a-z_]+)'")
HARNESS_ROLES = frozenset({"runner"})  # used_by có thể trỏ harness (repo-onboarding chạy bởi runner)


def _has_section(body: str, header: str) -> bool:
    return any(line.startswith(header) for line in body.splitlines())  # security-5c: "## Procedure — by role"


def check_skills(agents: AgentRegistry, skills: SkillRegistry, r: CheckReport) -> None:
    roles = set(agents.names()) | HARNESS_ROLES
    for w in skills.warnings:  # skill ở lớp override hỏng: đã bị bỏ qua, người cần biết
        r.warn(w)
    specs: list[SkillSpec] = [skills.get(n) for n in skills.names()]
    for s in specs:
        meta = s.meta
        cap = MAX_TOKENS.get(meta.source)
        if cap is None:
            r.err(f"skill {s.name}: source {meta.source!r} lạ (có: {sorted(MAX_TOKENS)})")
            cap = 1000
        if s.tokens > cap:
            r.warn(f"skill {s.name}: ~{s.tokens} token > cap {cap} của {meta.source} (registry nới 1.5x)")
        for role in meta.used_by:
            if role not in roles:
                r.err(f"skill {s.name}: used_by role lạ {role!r}")
        for header in REQUIRED_SECTIONS:
            if not _has_section(s.body, header):
                r.warn(f"skill {s.name}: thiếu mục `{header}`")
        base = s.rel.rsplit("/", 1)
        if base[-1] == SKILL_FILE:
            parent = base[0].rsplit("/", 1)[-1] if len(base) == 2 else ""
            if parent != s.name:
                r.warn(f"skill {s.name}: thư mục {parent!r} ≠ name (layout <name>/SKILL.md)")
        hits = injection_scan(
            s.body
        )  # harness cũng là bề mặt tấn công: skill text đi thẳng vào context agent
        if hits:
            r.err(f"skill {s.name}: body có pattern injection {hits[:2]} — người xem lại trước khi nạp")
        if meta.source == IMPORTED:
            if not meta.imported_at or not meta.adapted:
                r.warn(f"skill {s.name}: imported nên có imported_at + adapted: true")
            if SOURCE_LINE not in s.body:
                r.warn(
                    f"skill {s.name}: imported skill is missing the `{SOURCE_LINE} <url> (<license>)` footer"
                )

    for a in agents:
        always_tokens = 0
        for name in a.skills.always:
            sp = _ref(skills, a.name, name, r)
            if sp is not None:
                always_tokens += sp.tokens
                if a.name not in sp.meta.used_by:
                    r.err(f"agent {a.name}: khai skill {name} nhưng không nằm trong used_by của skill đó")
        worst = always_tokens
        by_type: dict[
            str, int
        ] = {}  # conditional theo `intake.type == 'x'` loại trừ nhau → chỉ tính nhóm nặng nhất
        for c in a.skills.conditional:
            if c.skill.startswith("learned/"):
                continue
            sp = _ref(skills, a.name, c.skill, r)
            if sp is not None:
                if a.name not in sp.meta.used_by:
                    r.err(f"agent {a.name}: khai skill {c.skill} nhưng không nằm trong used_by của skill đó")
                m = _TYPE_RE.search(c.when or "")
                if m:
                    by_type[m.group(1)] = by_type.get(m.group(1), 0) + sp.tokens
                else:
                    worst += sp.tokens
        worst += max(by_type.values(), default=0)
        if a.has_tool("user.ask"):
            worst += sum(
                s.tokens for s in skills.for_role(a.name) if s.is_manual and s not in _declared(a, skills)
            )
        if always_tokens > MAX_BRIEF_SKILL_TOKENS:
            r.err(
                f"agent {a.name}: skill always ~{always_tokens} token > trần brief {MAX_BRIEF_SKILL_TOKENS}"
            )
        elif worst > MAX_BRIEF_SKILL_TOKENS:
            r.warn(
                f"agent {a.name}: worst case ~{worst} token > trần {MAX_BRIEF_SKILL_TOKENS} (always + manual + conditional cùng khớp) — cái sau sẽ bị bỏ"
            )
        if always_tokens or worst:
            r.info(f"skills {a.name}: always ~{always_tokens} · worst ~{worst} / {MAX_BRIEF_SKILL_TOKENS}")


def _ref(skills: SkillRegistry, agent: str, name: str, r: CheckReport) -> SkillSpec | None:
    if not skills.has(name):
        r.err(f"agent {agent}: skill {name!r} không tồn tại (có: {skills.names()})")
        return None
    return skills.get(name)


def _declared(a: AgentSpec, skills: SkillRegistry) -> Iterable[SkillSpec]:
    names = [*a.skills.always, *(c.skill for c in a.skills.conditional)]
    return [skills.get(n) for n in names if skills.has(n)]

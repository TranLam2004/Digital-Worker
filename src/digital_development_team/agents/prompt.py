"""System prompt = JD (từ .agent.md) + preamble chung + Skills available (tên + 1 dòng) + Tools.

Ổn định theo role → prefix cache. Mọi thứ đổi theo issue (skill content, artifact) nằm ở brief (user message).
"""

from __future__ import annotations

from digital_development_team.agents.skills import SkillRegistry
from digital_development_team.agents.spec import AgentRegistry, AgentSpec
from digital_development_team.config.resources import AssetResolver

PREAMBLE_REL = "prompts/system_preamble.md"


def compose_system_prompt(
    spec: AgentSpec,
    agents: AgentRegistry,
    skills: SkillRegistry,
    resolver: AssetResolver,
    *,
    tool_descriptions: dict[str, str] | None = None,
) -> str:
    parts = [agents.jd(spec.name).rstrip(), "", resolver.read_text(PREAMBLE_REL).rstrip()]
    visible = skills.for_role(spec.name)
    if visible:
        parts += ["", "## Skills available", *(s.one_liner() for s in visible)]
        parts.append("(Full skill text appears in the brief CONTEXT only when its trigger matches.)")
    parts += ["", "## Tools"]
    for g in spec.tool_grants:
        desc = (tool_descriptions or {}).get(g.name, "")
        suffix = f" [{g.profile}]" if g.profile else ""
        parts.append(f"- {g.name}{suffix}" + (f" — {desc}" if desc else ""))
    parts += [
        "",
        f"Permission: {spec.permission_mode.value}. Output mode: {spec.output_mode.value}. "
        f"Writes: {', '.join(spec.writes)}. Reads: {', '.join(spec.reads) or '—'}.",
    ]
    return "\n".join(parts).strip() + "\n"

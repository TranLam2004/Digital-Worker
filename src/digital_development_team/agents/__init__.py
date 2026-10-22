"""Agents — 8 digital worker = 8 file `.agent.md` (frontmatter = config chạy được, Markdown = JD/system prompt).

agents = AgentRegistry(resolver)          # _assets/agents/*.agent.md (+ override layer)
spec = agents.get("dev")                   # AgentSpec: tools · reads · writes · output_mode · skills …
skills = SkillRegistry(resolver)           # _assets/skills/core/*.SKILL.md
system = compose_system_prompt(spec, skills, resolver)   # JD + preamble + skills (tên+1 dòng) + tools — prefix cache
brief = BriefBuilder(svc, cfg, skills, repo).build(spec, state)   # 4 khối, CONTEXT từ reads[] + context_extras
render_brief(brief, resolver)              # markdown user message
"""

from __future__ import annotations

from digital_development_team.agents.brief import BriefBuilder, render_brief
from digital_development_team.agents.prompt import compose_system_prompt
from digital_development_team.agents.repo_context import FsRepoContext, NullRepoContext, RepoContext
from digital_development_team.agents.skills import SkillRegistry, SkillSpec
from digital_development_team.agents.spec import AgentRegistry, AgentSpec, ToolGrant, parse_agent_file

__all__ = [
    "AgentRegistry",
    "AgentSpec",
    "BriefBuilder",
    "FsRepoContext",
    "NullRepoContext",
    "RepoContext",
    "SkillRegistry",
    "SkillSpec",
    "ToolGrant",
    "compose_system_prompt",
    "parse_agent_file",
    "render_brief",
]

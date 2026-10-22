"""Kích thước phần cố định mỗi lượt gọi model của từng vai — đo được KHÔNG cần gọi LLM.

system prompt (JD + preamble + danh sách skill/tool) · schema tool (MCP gửi kèm MỌI lượt) · skill luôn nạp vào brief.
Thi đấu chấm credit Codex: phần này nhân với số lượt → `ddt debug footprint` và contract test giữ nó không phình ra.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from digital_development_team.agents.prompt import compose_system_prompt
from digital_development_team.agents.skills import SkillRegistry
from digital_development_team.agents.spec import AgentRegistry
from digital_development_team.config.resources import AssetResolver
from digital_development_team.tools.catalog import ToolCatalog
from digital_development_team.util.text import estimate_tokens


@dataclass(frozen=True, slots=True)
class RoleFootprint:
    role: str
    system: int
    tools: int
    n_tools: int
    skills_always: int

    @property
    def per_call(self) -> int:
        """Token cố định mỗi lượt gọi model (chưa kể brief theo issue và lịch sử hội thoại)."""
        return self.system + self.tools


def footprint(
    agents: AgentRegistry, skills: SkillRegistry, resolver: AssetResolver, catalog: ToolCatalog
) -> list[RoleFootprint]:
    out: list[RoleFootprint] = []
    for spec in agents:
        if not spec.is_pipeline_agent:
            continue
        system = compose_system_prompt(spec, agents, skills, resolver)
        tool_specs = catalog.specs_for(spec.tool_names)
        schema = json.dumps(
            [
                {"name": t.wire_name, "description": t.description, "inputSchema": t.input_schema}
                for t in tool_specs
            ],
            ensure_ascii=False,
        )
        always = sum(s.tokens for s in skills.for_role(spec.name) if s.is_always)
        out.append(
            RoleFootprint(
                spec.name,
                estimate_tokens(system),
                estimate_tokens(schema) if tool_specs else 0,
                len(tool_specs),
                always,
            )
        )
    return out

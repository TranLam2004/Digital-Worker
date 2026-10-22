"""Contract: 8 agent .md + 7 skill trong _assets nhất quán với slot ACL, schema, models.yaml, budgets.yaml."""

from __future__ import annotations

import pytest

from digital_development_team.agents import AgentRegistry, SkillRegistry
from digital_development_team.blackboard.slots import DEFAULT_ACL, SLOTS, SlotAcl
from digital_development_team.config.loader import load_config
from digital_development_team.core.enums import PermissionMode, Stage
from digital_development_team.schema import SLOT_SCHEMA

PIPELINE_STAGES = {
    Stage.TRIAGE,
    Stage.LOCALIZE,
    Stage.SPEC,
    Stage.IMPLEMENT,
    Stage.VERIFY,
    Stage.REVIEW,
    Stage.GATE,
}


@pytest.fixture(scope="module")
def agents() -> AgentRegistry:
    return AgentRegistry()


@pytest.fixture(scope="module")
def skills() -> SkillRegistry:
    return SkillRegistry()


def test_eight_agents_cover_pipeline(agents: AgentRegistry) -> None:
    assert agents.names() == [
        "analyst",
        "dev",
        "intake",
        "leader",
        "learner",
        "planner",
        "security",
        "verifier",
    ]
    assert {a.primary_stage for a in agents.pipeline_agents()} == PIPELINE_STAGES
    for st in PIPELINE_STAGES:
        agents.for_stage(st)  # đúng 1 agent / stage
    assert agents.get("learner").primary_stage is None and agents.get("leader").stages == ["GATE", "TRIAGE"]


def test_reads_writes_within_acl_matrix(agents: AgentRegistry) -> None:
    acl = SlotAcl()
    for a in agents.pipeline_agents():
        acl2 = acl.with_role(a.name, reads=a.read_slots, writes=a.writes)
        assert acl2.violations_against_default(a.name) == [], a.name
        assert a.output_slot in SLOTS
        if a.output_slot != "patch/":
            assert a.output_slot in SLOT_SCHEMA, a.name
        assert "escalation.yaml" in a.writes  # đường thoát
        assert a.name in DEFAULT_ACL


def test_independence_rules(agents: AgentRegistry) -> None:
    v = agents.get("verifier")
    # không thấy gì Dev viết ngoài diff; match.json = MVS/đề của khách hàng, không phải lời Dev
    assert v.read_slots == ["spec.yaml", "patch/", "match.json"]
    assert v.condenser.memory_block_tokens == 0 and not any(t.startswith("memory.") for t in v.tools)
    assert v.model_family_must_differ_from == "dev" and v.clarify.can_ask == []
    dev = agents.get("dev")
    assert "intake.yaml" not in dev.read_slots and dev.permission_mode is PermissionMode.ACCEPT_EDITS
    assert all("git.push" not in a.tools for a in agents)
    assert (
        agents.get("leader").tool_profile("blackboard.read") is None
        and "patch/" in agents.get("leader").read_slots
    )
    assert agents.get("learner").permission_mode is PermissionMode.MEMORY_ONLY


def test_models_budgets_skills_resolve(agents: AgentRegistry, skills: SkillRegistry) -> None:
    cfg = load_config()
    for a in agents:
        assert a.name in cfg.models.roles, f"models.yaml#roles thiếu {a.name}"
        assert cfg.models.aliases.get(a.model) is not None, f"{a.name}: alias {a.model} null"
        if a.fallback_model:
            assert cfg.models.aliases.get(a.fallback_model) is not None
        assert a.budget_ref in cfg.budgets.stages, f"{a.name}: budget_ref {a.budget_ref}"
        for s in a.skills.always:
            assert skills.has(s), f"{a.name}: skill {s}"
        for c in a.skills.conditional:
            if not c.skill.startswith("learned/"):
                assert skills.has(c.skill), f"{a.name}: skill {c.skill}"
                assert a.name in skills.get(c.skill).meta.used_by
        for g in a.tool_grants:
            assert g.namespace in {
                "fs",
                "exec",
                "test",
                "git",
                "codegraph",
                "security",
                "blackboard",
                "memory",
                "user",
                "agent",
                "llm",
                "web",
                "docs",
                "skill",
                "pkg",
            }, g


CORE_SKILLS = [
    "adversarial-review",
    "beyond-mvs",
    "escalation-form",
    "localize-3-tier",
    "pi-investigation",
    "repo-onboarding",
    "security-5c",
    "self-review",
    "spec-writing",
]
IMPORTED_SKILLS = [
    "ask-questions-if-underspecified",
    "differential-review",
    "py-review",
    "systematic-debugging",
    "test-driven-development",
]


def test_skills_catalog(skills: SkillRegistry) -> None:
    assert skills.names() == sorted(CORE_SKILLS + IMPORTED_SKILLS)
    for s in (skills.get(n) for n in skills.names()):
        assert s.tokens <= 1500 and s.meta.source in {"core", "imported"}
        for sec in ("## When to apply", "## Procedure", "## Known pitfalls", "## Output checklist"):
            assert sec in s.body, f"{s.name} thiếu {sec}"
    assert skills.get("adversarial-review").is_always and [x.name for x in skills.for_role("verifier")] == [
        "adversarial-review",
        "beyond-mvs",
        "differential-review",
        "py-review",
    ]
    assert skills.get("escalation-form").is_manual


def test_imported_skills_provenance(skills: SkillRegistry) -> None:
    """Skill import: layout <name>/SKILL.md · origin pin GitHub · license · adapted · dòng Nguồn · ≤ cap imported."""
    for name in IMPORTED_SKILLS:
        s = skills.get(name)
        assert s.meta.source == "imported" and s.rel == f"skills/imported/{name}/SKILL.md"
        assert s.meta.origin and s.meta.origin.startswith("https://github.com/")
        assert s.meta.license in {"MIT", "CC-BY-SA-4.0"} and s.meta.adapted and s.meta.imported_at
        assert "Source:" in s.body and s.tokens <= 1000, (name, s.tokens)
        # không được kéo skill khác vào brief qua chuỗi (tests/unit/agents/test_brief.py)
        assert (
            "security-5c" not in s.body
            and "KINH NGHIỆM" not in s.body
            and "RELEVANT EXPERIENCE" not in s.body
        )
    for n in CORE_SKILLS:
        assert skills.get(n).meta.source == "core"

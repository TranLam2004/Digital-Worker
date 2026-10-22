"""Credit Codex là tiêu chí xếp hạng thứ 3: phần cố định mỗi lượt gọi model không được phình ra (cấu hình --match)."""

from __future__ import annotations

from pathlib import Path

from digital_development_team.agents import AgentRegistry, SkillRegistry
from digital_development_team.agents.footprint import footprint
from digital_development_team.app import build_runtime, build_tooling, mcp_grants

# trần ≈ ký tự/4 — đo 2026-10-16 sau khi gọn JD/preamble và gỡ tool không ra điểm (+~10% chỗ thở)
CEILING = {"dev": 4500, "planner": 4400, "verifier": 5000, "security": 5400, "analyst": 3800}


def test_per_call_footprint_under_ceiling(tmp_path: Path) -> None:
    rt = build_runtime(tmp_path / "h.ddt", flags=["--match"])
    try:
        tooling = build_tooling(rt, base_ref=lambda _t: "main")
        reg = AgentRegistry(rt.resolver)
        reg.grant(mcp_grants(rt))
        reg.revoke(rt.config.policy.tool_denylist)
        got = {f.role: f for f in footprint(reg, SkillRegistry(rt.resolver), rt.resolver, tooling.catalog)}
    finally:
        rt.close()
    for role, ceiling in CEILING.items():
        assert got[role].per_call <= ceiling, f"{role}: {got[role]} > {ceiling}"
    assert "agent_delegate" not in str(got["dev"])  # tool stub không gửi kèm
    assert got["dev"].n_tools <= 17

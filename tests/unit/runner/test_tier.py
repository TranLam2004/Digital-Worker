from __future__ import annotations

from typing import Any

from digital_development_team.config.loader import load_config
from digital_development_team.core.enums import Tier
from digital_development_team.risk import compute_tier

LIMITS = {"conf_bump_lo": 0.5, "conf_bump_hi": 0.8}


def _scope(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"files_touched": [], "sensitive_paths": ["**/auth/**", ".env"], "limits": LIMITS}
    base.update(kw)
    return base


def test_default_and_value_rules() -> None:
    rules = load_config().policy.tier_rules
    r = compute_tier(rules, _scope())
    assert r.tier is Tier.T1 and r.reasons == () and any("intake" in s for s in r.skipped)
    r = compute_tier(rules, _scope(intake={"type": "bug", "risk_tier": "T0"}))
    assert r.tier is Tier.T0 and r.reasons == ("intake.risk_tier=T0",)
    r = compute_tier(rules, _scope(intake={"type": "bug", "risk_tier": "T0"}, spec={"estimated_tier": "T2"}))
    assert r.tier is Tier.T2  # MAX


def test_security_incident_sensitive_and_bumps() -> None:
    rules = load_config().policy.tier_rules
    assert (
        compute_tier(rules, _scope(intake={"type": "security_incident", "risk_tier": "T1"})).tier is Tier.T3
    )
    r = compute_tier(
        rules, _scope(intake={"type": "bug", "risk_tier": "T1"}, files_touched=["src/auth/login.py"])
    )
    assert r.tier is Tier.T3 and any("sensitive_paths" in x for x in r.reasons)
    r = compute_tier(
        rules, _scope(intake={"type": "bug", "risk_tier": "T1"}, files_touched=[f"f{i}.py" for i in range(6)])
    )
    assert r.tier is Tier.T2
    # confidence trong vùng bump → +1
    r = compute_tier(rules, _scope(intake={"type": "bug", "risk_tier": "T1"}, confidence=0.6))
    assert r.tier is Tier.T2 and any("+1" in x for x in r.reasons)
    # không bao giờ hạ
    assert (
        compute_tier(rules, _scope(intake={"type": "bug", "risk_tier": "T0"}), current=Tier.T2).tier
        is Tier.T2
    )
    # +1 kẹt trần
    assert (
        compute_tier(
            rules, _scope(intake={"type": "security_incident", "risk_tier": "T3"}, confidence=0.6)
        ).tier
        is Tier.T3
    )

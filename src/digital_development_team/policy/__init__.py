"""Policy Engine — tool call nào được phép (10-harness/04). Tra bảng, 0 LLM. Policy là dữ liệu (policy.yaml).

4 lớp: 1 hooks (code tất định) → 2 static rules (policy.yaml#tool_rules) → 3 permission mode → 4 human G2.
P1: lớp 1–3; lớp 4 (G2 ask) → deny có hint (TODO P2#16 session approvals + user.ask pause).

    engine = PolicyEngine(cfg.policy, cfg.sensitive_paths, catalog)
    d = engine.decide(call, spec, PolicyContext(...))      # Decision(action, rule_id, reason, hint, constraint, risk_tier)
    result = engine.after(call, result)                      # redact secret · injection scan
"""

from __future__ import annotations

from digital_development_team.policy.engine import Decision, PolicyContext, PolicyEngine
from digital_development_team.policy.hooks import diff_sanity, injection_scan, redact_secrets

__all__ = ["Decision", "PolicyContext", "PolicyEngine", "diff_sanity", "injection_scan", "redact_secrets"]

"""Risk — tier (P1) · confidence (P2#20) · canary (P3#29).

tier, reasons = compute_tier(cfg.policy.tier_rules, scope, current=state.tier)
# MAX mọi rule khớp, không bao giờ hạ. Rule thiếu dữ liệu → bỏ qua, đánh giá lại sau mỗi stage.
"""

from __future__ import annotations

from digital_development_team.risk.tier import TierResult, compute_tier

__all__ = ["TierResult", "compute_tier"]

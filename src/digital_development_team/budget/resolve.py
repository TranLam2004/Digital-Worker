"""budgets.yaml → BudgetRef tuyệt đối cho một lần chạy stage.

- rework_bump: N → lần rework thứ ≥1 dùng budget của tier +N (Dev lần 2 được rộng hơn, không vô hạn).
- change_request_multiplier: issue type change_request → nhân turn & tokens.
- Stage không có trong budgets.yaml (INGEST, MACHINE_GATE, MERGE, *_WAIT) = 0 LLM → None.
"""

from __future__ import annotations

from digital_development_team.config.models import BudgetsConfig, IssueBudget
from digital_development_team.core.context import BudgetRef
from digital_development_team.core.enums import Stage, Tier


def resolve_stage_budget(
    budgets: BudgetsConfig,
    stage: Stage,
    tier: Tier,
    *,
    rework: int = 0,
    is_change_request: bool = False,
    multiplier: float = 1.0,
) -> BudgetRef | None:
    sb = budgets.stages.get(stage.value)
    if sb is None:
        return None
    eff_tier = tier.bump(sb.rework_bump) if (sb.rework_bump and rework > 0) else tier
    b = sb.for_tier(eff_tier)
    turns, tokens = b.turn, b.tokens
    if is_change_request and sb.change_request_multiplier:
        turns = round(turns * sb.change_request_multiplier)
        tokens = round(tokens * sb.change_request_multiplier)
    if multiplier != 1.0:  # profile loại task (policy.yaml#task_types.<type>.budget_multiplier)
        turns, tokens = round(turns * multiplier), round(tokens * multiplier)
    return BudgetRef(turns=max(turns, 1), wall_s=b.wall * 60.0 * max(multiplier, 1.0), tokens=max(tokens, 1))


def apply_transport_overhead(ref: BudgetRef, budgets: BudgetsConfig, transport: str) -> BudgetRef:
    """Trần stage + phụ phí theo transport × số lượt (budgets.yaml#transport_overhead). Không có cấu hình → giữ nguyên."""
    over = budgets.transport_overhead.get(transport)
    if over is None or (over.tokens_per_turn == 0 and over.wall_s_per_turn == 0 and over.turn_factor == 1.0):
        return ref
    turns = max(round(ref.turns * over.turn_factor), ref.turns)
    return BudgetRef(
        turns=turns,
        wall_s=ref.wall_s + over.wall_s_per_turn * turns,
        tokens=ref.tokens + over.tokens_per_turn * turns,
    )


def issue_budget(budgets: BudgetsConfig, tier: Tier, *, transport: str = "api") -> IssueBudget:
    """Cap issue theo tier. transport != api → cộng phụ phí `transport_overhead.<transport>` × Σ turn cap mọi stage
    của tier (cùng cơ chế apply_transport_overhead cho stage; không có cấu hình → giữ nguyên)."""
    ib = budgets.issue[tier.value]
    over = budgets.transport_overhead.get(transport) if transport != "api" else None
    if over is None or (over.tokens_per_turn == 0 and over.wall_s_per_turn == 0):
        return ib
    turns = 0
    for sb in budgets.stages.values():
        try:
            turns += sb.for_tier(tier).turn
        except ValueError:
            continue
    turns = round(turns * over.turn_factor)
    return IssueBudget(
        tokens=ib.tokens + over.tokens_per_turn * turns,
        wall=round(ib.wall + over.wall_s_per_turn * turns / 60.0, 2),
    )

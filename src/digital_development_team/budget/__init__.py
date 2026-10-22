"""Budget — hard cap theo stage × tier (budgets.yaml). Chạm → BudgetExceeded → circuit breaker.

ref = resolve_stage_budget(cfg.budgets, Stage.IMPLEMENT, Tier.T1, rework=1)   # BudgetRef(turns, wall_s, tokens)
meter = BudgetMeter(ref, clock)
meter.add(usage); meter.check()        # raise BudgetExceeded(scope="stage", axis="tokens")
meter.crossed()                        # [60, 80] mức cảnh báo mới vượt → emit budget_warning
"""

from __future__ import annotations

from digital_development_team.budget.meter import BudgetMeter, IssueMeter
from digital_development_team.budget.resolve import issue_budget, resolve_stage_budget

__all__ = ["BudgetMeter", "IssueMeter", "issue_budget", "resolve_stage_budget"]

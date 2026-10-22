from __future__ import annotations

import pytest

from digital_development_team.budget import BudgetMeter, IssueMeter, issue_budget, resolve_stage_budget
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import FakeClock
from digital_development_team.core.enums import Stage, Tier
from digital_development_team.core.errors import BudgetExceeded
from digital_development_team.core.models import BudgetUsage


def test_resolve_stage_budget() -> None:
    b = load_config().budgets
    r = resolve_stage_budget(b, Stage.IMPLEMENT, Tier.T1)
    assert r is not None and (r.turns, r.wall_s, r.tokens) == (25, 12 * 60, 150_000)
    # rework_bump: 1 → dùng budget T2
    r2 = resolve_stage_budget(b, Stage.IMPLEMENT, Tier.T1, rework=1)
    assert r2 is not None and r2.turns == 30
    assert resolve_stage_budget(b, Stage.IMPLEMENT, Tier.T3, rework=2) is not None  # bump kẹt trần T3
    # change_request_multiplier cho SPEC
    r3 = resolve_stage_budget(b, Stage.SPEC, Tier.T1, is_change_request=True)
    assert r3 is not None and r3.turns == 4 and r3.tokens == 30_000
    # stage "any"
    assert resolve_stage_budget(b, Stage.ANSWER, Tier.T3) is not None
    # stage 0 LLM
    assert resolve_stage_budget(b, Stage.MACHINE_GATE, Tier.T1) is None
    assert issue_budget(b, Tier.T0).tokens == 200_000


def test_meter_check_and_warn(fake_clock: FakeClock) -> None:
    b = load_config().budgets
    ref = resolve_stage_budget(b, Stage.LOCALIZE, Tier.T1)  # turn 3 · wall 2 phút · 30k
    assert ref is not None
    m = BudgetMeter(ref, fake_clock)
    m.add_turn(2000, 500)  # turns 1/3, tokens 2500/30000
    assert m.crossed() == []
    m.add(BudgetUsage(tokens_in=18_500))  # 21000/30000 = 70%
    assert m.crossed() == [60]
    assert m.crossed() == []  # chỉ báo 1 lần
    assert m.remaining_tokens() == 9_000 and m.remaining_turns() == 2
    m.add_turn(1, 1)
    m.check()
    with pytest.raises(BudgetExceeded) as ei:
        m.add_turn(1, 1)  # turns 3 ≥ 3
        m.check()
    assert ei.value.axis == "turns" and ei.value.scope == "stage"
    # wall
    m2 = BudgetMeter(ref, fake_clock)
    fake_clock.advance(121)
    with pytest.raises(BudgetExceeded, match="wall"):
        m2.check()


def test_issue_meter_resumes_usage(fake_clock: FakeClock) -> None:
    b = load_config().budgets
    im = IssueMeter(issue_budget(b, Tier.T0), fake_clock, usage=BudgetUsage(tokens_in=199_000, wall_s=100))
    assert im.wall_s == pytest.approx(100)
    im.add(BudgetUsage(tokens_out=1000))
    with pytest.raises(BudgetExceeded) as ei:
        im.check()
    assert ei.value.scope == "issue" and ei.value.axis == "tokens"
    assert im.snapshot()["tokens_max"] == 200_000


def test_cli_final_turn_at_cap_is_not_killed(fake_clock: FakeClock) -> None:
    """Live 2026-10-05: ANSWER trần 2 lượt, lượt 2 là câu trả lời JSON → CLI bị kill, kết quả bị vứt."""
    from digital_development_team.agents.loop import MAX_FINAL_ATTEMPTS, _cli_may_continue

    ref = resolve_stage_budget(load_config().budgets, Stage.ANSWER, Tier.T1)
    assert ref is not None and ref.turns == 2
    m = BudgetMeter(ref, fake_clock)
    m.add_turn(1000, 10)
    assert _cli_may_continue(m, ("mcp__ddt__fs_read",))  # còn trần → tiếp
    m.add_turn(1000, 10)  # chạm trần
    assert _cli_may_continue(m, ())  # lượt trả lời cuối → để process kết thúc
    assert _cli_may_continue(m, ("mcp__ddt__blackboard_write",))  # lượt ghi output
    assert not _cli_may_continue(m, ("mcp__ddt__fs_read",))  # còn đòi làm việc → kill
    for _ in range(MAX_FINAL_ATTEMPTS + 1):
        m.add_turn(1, 1)
    assert not _cli_may_continue(m, ("mcp__ddt__blackboard_write",))  # sửa schema quá số lần → kill

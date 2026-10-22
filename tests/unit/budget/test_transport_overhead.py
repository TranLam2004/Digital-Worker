"""transport_overhead: role đi CLI → trần stage + phụ phí × turn; API giữ nguyên; thiếu cấu hình → giữ nguyên."""

from __future__ import annotations

from digital_development_team.budget.resolve import apply_transport_overhead, resolve_stage_budget
from digital_development_team.config.loader import load_config
from digital_development_team.core.enums import Stage, Tier


def test_cli_overhead_added_per_turn() -> None:
    cfg = load_config()
    base = resolve_stage_budget(cfg.budgets, Stage.GATE, Tier.T1)
    assert base is not None
    over = cfg.budgets.transport_overhead["cli"]
    cli = apply_transport_overhead(base, cfg.budgets, "cli")
    assert cli.turns == base.turns
    assert cli.tokens == base.tokens + over.tokens_per_turn * base.turns
    assert cli.wall_s == base.wall_s + over.wall_s_per_turn * base.turns
    assert apply_transport_overhead(base, cfg.budgets, "api") == base  # không khai báo → giữ nguyên


def test_registry_reports_transport() -> None:
    from digital_development_team.llm.registry import ModelRegistry

    cfg = load_config(preset="cli_all")
    reg = ModelRegistry(cfg.models, env={}, providers={"anthropic_cli": object()})  # type: ignore[dict-item]
    assert reg.transport_for("dev") == "cli" and reg.transport_of("anthropic") == "api"
    assert ModelRegistry(load_config().models, env={"ANTHROPIC_API_KEY": "x"}).transport_for("dev") == "api"


def test_issue_budget_cli_overhead() -> None:
    from digital_development_team.budget.resolve import issue_budget

    cfg = load_config()
    base = issue_budget(cfg.budgets, Tier.T0)
    cli = issue_budget(cfg.budgets, Tier.T0, transport="cli")
    over = cfg.budgets.transport_overhead["cli"]
    turns = sum(sb.for_tier(Tier.T0).turn for sb in cfg.budgets.stages.values() if sb.for_tier(Tier.T0))
    assert (
        cli.tokens == base.tokens + over.tokens_per_turn * turns and cli.tokens > 156_488
    )  # DDT-0002 live đã chạm 156k
    assert cli.wall > base.wall and issue_budget(cfg.budgets, Tier.T0, transport="api") == base


def test_codex_turn_factor_widens_turn_cap() -> None:
    """Codex báo TỪNG tool call là một lượt → REVIEW T1 (1 lượt) / LOCALIZE (3) không bị kill sau 1–3 lời gọi tool."""
    from digital_development_team.llm.registry import ModelRegistry

    cfg = load_config(preset="codex_all")
    over = cfg.budgets.transport_overhead["codex"]
    for stage in (Stage.REVIEW, Stage.LOCALIZE):
        base = resolve_stage_budget(cfg.budgets, stage, Tier.T1)
        assert base is not None
        codex = apply_transport_overhead(base, cfg.budgets, "codex")
        assert codex.turns == round(base.turns * over.turn_factor) > base.turns
        assert codex.tokens == base.tokens + over.tokens_per_turn * codex.turns
    reg = ModelRegistry(cfg.models, env={}, providers={"codex_cli": object()})  # type: ignore[dict-item]
    assert reg.overhead_key_for("dev") == "codex" and reg.transport_for("dev") == "cli"

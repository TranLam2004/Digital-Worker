"""Config đóng gói phải pass `ddt config check` — không có lỗi, chỉ cảnh báo đã biết."""

from __future__ import annotations

from typer.testing import CliRunner

from digital_development_team.cli.main import app
from digital_development_team.config.checks import run_checks
from digital_development_team.config.loader import load_config
from digital_development_team.config.resources import build_resolver
from digital_development_team.core.enums import Stage

KNOWN_WARNINGS = ("sandbox.kind=local", "cùng họ model", "thiếu binary")


def test_default_config_passes_checks() -> None:
    report = run_checks(load_config())
    assert report.ok, report.errors
    for w in report.warnings:
        assert any(k in w for k in KNOWN_WARNINGS), f"cảnh báo lạ: {w}"


def test_every_pipeline_stage_has_transition_table() -> None:
    cfg = load_config()
    for s in Stage:
        if s.terminal:
            continue
        assert s.value in cfg.policy.transitions, f"thiếu transitions.{s.value}"


def test_cli_config_check_exit_zero() -> None:
    result = CliRunner().invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    assert "config OK" in result.output


def test_cli_config_roles() -> None:
    result = CliRunner().invoke(app, ["config", "roles"])
    assert result.exit_code == 0, result.output
    assert "verifier" in result.output


def test_assets_readable_via_resources() -> None:
    res = build_resolver()
    for rel in (
        "config/policy.yaml",
        "config/budgets.yaml",
        "config/models.yaml",
        "config/sensitive_paths.yaml",
    ):
        assert res.exists(rel), rel
        assert len(res.read_text(rel)) > 100

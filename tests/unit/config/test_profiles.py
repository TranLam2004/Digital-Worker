from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from digital_development_team.cli.main import app
from digital_development_team.config.loader import load_config


@pytest.mark.parametrize("mode", ["daily", "contest"])
def test_profile_command_creates_isolated_valid_codex_config(tmp_path: Path, mode: str) -> None:
    home = tmp_path / mode
    args = [
        "--home",
        str(home),
        "config",
        "profile",
        mode,
        "--fast-model",
        "gpt-test-fast",
        "--main-model",
        "gpt-test-main",
        "--strong-model",
        "gpt-test-strong",
        "--budget-usd",
        "3",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    cfg = load_config(home=home)
    assert cfg.models.aliases["sonnet"].model == "gpt-test-main"
    assert cfg.models.aliases["opus"].provider == "codex_cli"
    assert cfg.policy.autonomy == ("autonomous" if mode == "contest" else "conservative")
    assert cfg.budgets.session.usd == 3
    assert cfg.policy.no_learn == (mode == "contest")
    assert CliRunner().invoke(app, args).exit_code != 0  # a second setup cannot overwrite the first

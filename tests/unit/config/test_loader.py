from __future__ import annotations

from pathlib import Path

import pytest

from digital_development_team.config.checks import run_checks
from digital_development_team.config.loader import apply_dotted, load_config
from digital_development_team.config.provider import ConfigProvider
from digital_development_team.config.resources import build_resolver
from digital_development_team.core.enums import Tier
from digital_development_team.core.errors import ConfigError


def _override_dir(tmp_path: Path) -> Path:
    """--config-dir mirror cấu trúc _assets → file nằm ở <dir>/config/*.yaml."""
    d = tmp_path / "cfg" / "config"
    d.mkdir(parents=True)
    return d.parent


def _packaged(rel: str) -> str:
    return build_resolver().read_text(rel)


def test_packaged_loads_and_checks_pass() -> None:
    cfg = load_config()
    assert cfg.policy.wip_limit == 3
    assert cfg.budgets.issue["T1"].tokens == 500_000
    assert cfg.models.providers["anthropic"].enabled
    assert set(cfg.sources.values()) == {"packaged"}
    report = run_checks(cfg)
    assert report.ok, report.errors


def test_apply_dotted() -> None:
    d: dict = {"a": {"b": 1}}
    apply_dotted(d, "a.c.d", 5)
    apply_dotted(d, "x", 1)
    assert d == {"a": {"b": 1, "c": {"d": 5}}, "x": 1}


def test_flags_patch() -> None:
    cfg = load_config(flags=["--minimal", "--wip", "1", "--no-canary", "--conservative"])
    assert cfg.policy.minimal and cfg.policy.no_learn
    assert cfg.policy.wip_limit == 1
    assert not cfg.policy.canary.enabled
    assert cfg.policy.autonomy == "conservative"
    assert any("--minimal" in p for p in cfg.patches)


def test_unknown_flag_rejected() -> None:
    with pytest.raises(ConfigError, match="flag lạ"):
        load_config(flags=["--yolo"])


def test_preset_mock_all() -> None:
    cfg = load_config(preset="mock_all")
    assert cfg.models.aliases["opus"].provider == "mock"  # type: ignore[union-attr]
    assert run_checks(cfg).ok


def test_economy_mode_applies() -> None:
    cfg = load_config(flags=["--economy"])
    assert cfg.policy.wip_limit == 1
    assert cfg.models.roles["dev"].model == "sonnet"
    assert cfg.budgets.stages["IMPLEMENT"].for_tier(Tier.T1).tokens == 100_000
    assert cfg.policy.condenser_defaults.memory_block_tokens == 600
    assert run_checks(cfg).ok


def test_override_layer_replaces_whole_file(tmp_path: Path) -> None:
    cfg_dir = _override_dir(tmp_path)
    (cfg_dir / "config" / "policy.yaml").write_text(
        _packaged("config/policy.yaml").replace("wip_limit: 3", "wip_limit: 2"), encoding="utf-8"
    )
    cfg = load_config(config_dir=cfg_dir)
    assert cfg.policy.wip_limit == 2
    assert cfg.sources["config/policy.yaml"] == "config_dir"
    assert cfg.sources["config/budgets.yaml"] == "packaged"


def test_home_overrides_layer(tmp_path: Path) -> None:
    home = tmp_path / "repo.ddt"
    (home / "overrides" / "config").mkdir(parents=True)
    (home / "overrides" / "config" / "policy.yaml").write_text(
        _packaged("config/policy.yaml").replace("wip_limit: 3", "wip_limit: 5"), encoding="utf-8"
    )
    cfg = load_config(home=home)
    assert cfg.policy.wip_limit == 5
    assert cfg.sources["config/policy.yaml"] == "home"


def test_config_dir_beats_home(tmp_path: Path) -> None:
    home = tmp_path / "repo.ddt"
    (home / "overrides" / "config").mkdir(parents=True)
    (home / "overrides" / "config" / "policy.yaml").write_text(
        _packaged("config/policy.yaml").replace("wip_limit: 3", "wip_limit: 5"), encoding="utf-8"
    )
    cfg_dir = _override_dir(tmp_path)
    (cfg_dir / "config" / "policy.yaml").write_text(
        _packaged("config/policy.yaml").replace("wip_limit: 3", "wip_limit: 2"), encoding="utf-8"
    )
    cfg = load_config(home=home, config_dir=cfg_dir)
    assert cfg.policy.wip_limit == 2


def test_invalid_yaml_in_override_is_config_error(tmp_path: Path) -> None:
    cfg_dir = _override_dir(tmp_path)
    (cfg_dir / "config" / "policy.yaml").write_text("wip_limit: 'không phải số'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"policy\.yaml không hợp lệ"):
        load_config(config_dir=cfg_dir)


def test_provider_snapshot_reload_and_keep_old_on_error(tmp_path: Path) -> None:
    cfg_dir = _override_dir(tmp_path)
    pol = cfg_dir / "config" / "policy.yaml"
    errors: list[ConfigError] = []
    prov = ConfigProvider(home=None, config_dir=cfg_dir, on_error=errors.append)
    c1 = prov.snapshot()
    assert c1.policy.wip_limit == 3
    pol.write_text(_packaged("config/policy.yaml").replace("wip_limit: 3", "wip_limit: 4"), encoding="utf-8")
    c2 = prov.snapshot()
    assert c2.policy.wip_limit == 4 and c2.fingerprint != c1.fingerprint
    pol.write_text("wip_limit: [bad\n", encoding="utf-8")
    c3 = prov.snapshot()
    assert c3.policy.wip_limit == 4  # giữ bản cũ
    assert errors


def test_sensitive_min_tier() -> None:
    cfg = load_config()
    assert cfg.sensitive_paths.min_tier_for("src/auth/session.py") == Tier.T3
    assert cfg.sensitive_paths.min_tier_for("src/api/v1/users.py") == Tier.T2
    assert cfg.sensitive_paths.min_tier_for("README.md") is None


def test_budget_check_catches_low_cap(tmp_path: Path) -> None:
    cfg_dir = _override_dir(tmp_path)
    txt = _packaged("config/budgets.yaml").replace(
        "T1: { tokens: 500000,  wall: 40 }", "T1: { tokens: 250000,  wall: 40 }"
    )
    assert "250000" in txt
    (cfg_dir / "config" / "budgets.yaml").write_text(txt, encoding="utf-8")
    report = run_checks(load_config(config_dir=cfg_dir))
    assert not report.ok
    assert any("budgets.issue.T1" in e for e in report.errors)

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from digital_development_team.config.models import GameMasterCfg
from digital_development_team.core.context import FakeClock
from digital_development_team.core.errors import ConfigError
from digital_development_team.gamemaster.fake import FakeGameMaster
from digital_development_team.gamemaster.player import load_player


def test_explicit_adapter_receives_local_configuration(monkeypatch, tmp_path: Path) -> None:
    module = ModuleType("contest_test_bridge")
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return FakeGameMaster({"match_id": kwargs["match_id"]}, clock=kwargs["clock"])

    module.create = create
    monkeypatch.setitem(sys.modules, module.__name__, module)
    cfg = GameMasterCfg(adapter_factory="contest_test_bridge:create")
    player = load_player(cfg, "M", clock=FakeClock(), repo=tmp_path)
    assert player.match_id == "M" and seen["repo"] == tmp_path and seen["config"] == cfg


def test_incomplete_adapter_is_rejected(monkeypatch) -> None:
    module = ModuleType("contest_bad_bridge")
    module.create = lambda **kw: SimpleNamespace(match_id=kw["match_id"])
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(ConfigError, match="port"):
        load_player(
            GameMasterCfg(adapter_factory="contest_bad_bridge:create"), "M", clock=FakeClock(), repo=None
        )

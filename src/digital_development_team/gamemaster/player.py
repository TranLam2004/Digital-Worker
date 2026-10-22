"""Explicit adapter seam for the organizer's Game Player; no guessed transport."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import cast

from digital_development_team.config.models import GameMasterCfg
from digital_development_team.core.context import Clock
from digital_development_team.core.errors import ConfigError
from digital_development_team.gamemaster.port import GameMaster


def load_player(cfg: GameMasterCfg, match_id: str, *, clock: Clock, repo: Path | None) -> GameMaster:
    module_name, separator, function_name = (cfg.adapter_factory or "").partition(":")
    if not separator or not module_name or not function_name.isidentifier():
        raise ConfigError("Game Player adapter_factory must be module:function")
    try:
        factory = getattr(importlib.import_module(module_name), function_name)
        player = factory(config=cfg, match_id=match_id, clock=clock, repo=repo)
    except (ImportError, AttributeError, TypeError) as e:
        raise ConfigError(f"Cannot load Game Player adapter: {e}") from e
    if getattr(player, "match_id", None) != match_id or any(
        not callable(getattr(player, name, None)) for name in ("snapshot", "start_working", "submit", "submission")
    ):
        raise ConfigError("Game Player adapter does not implement the team-authorized GameMaster port")
    return cast(GameMaster, player)

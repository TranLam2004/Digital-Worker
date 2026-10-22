"""ConfigProvider: snapshot() trả ResolvedConfig; reload khi mtime file override đổi; file hỏng → giữ bản cũ.

Runner gọi snapshot() ở đầu mỗi stage → một stage thấy một config nhất quán.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from digital_development_team.config.loader import FILES, ResolvedConfig, load_config
from digital_development_team.core.errors import ConfigError

log = logging.getLogger(__name__)


class ConfigProvider:
    def __init__(
        self,
        *,
        home: Path | None,
        config_dir: Path | None,
        flags: Sequence[str] = (),
        preset: str | None = None,
        on_reload: Callable[[ResolvedConfig], None] | None = None,
        on_error: Callable[[ConfigError], None] | None = None,
    ) -> None:
        self._home = home
        self._config_dir = config_dir
        self._flags = tuple(flags)
        self._preset = preset
        self._on_reload = on_reload
        self._on_error = on_error
        self._lock = threading.Lock()
        self._current: ResolvedConfig | None = None
        self.last_error: str | None = None  # lỗi config gần nhất (giữ bản cũ); dashboard hiện banner
        self._mtimes: dict[str, float] = {}

    def _watch_paths(self) -> list[Path]:
        from digital_development_team.config.resources import HOME_OVERRIDES_DIR

        paths: list[Path] = []
        if self._home:
            paths.append(self._home / HOME_OVERRIDES_DIR / "config" / "runtime.yaml")
        for rel in FILES.values():
            if self._config_dir:
                paths.append(self._config_dir / rel)
            if self._home:
                paths.append(self._home / HOME_OVERRIDES_DIR / rel)
        return paths

    def _mtime_map(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self._watch_paths():
            with contextlib.suppress(OSError):
                out[str(p)] = p.stat().st_mtime
        return out

    def snapshot(self) -> ResolvedConfig:
        with self._lock:
            mt = self._mtime_map()
            if self._current is not None and mt == self._mtimes:
                return self._current
            try:
                cfg = load_config(
                    home=self._home, config_dir=self._config_dir, flags=self._flags, preset=self._preset
                )
            except ConfigError as e:
                if self._current is None:
                    raise
                log.error("config invalid, giữ bản cũ: %s", e)
                self.last_error = e.message + (f" ({e.hint})" if e.hint else "")
                if self._on_error:
                    self._on_error(e)
                self._mtimes = mt
                return self._current
            changed = self._current is not None and cfg.fingerprint != self._current.fingerprint
            self.last_error = None
            self._current = cfg
            self._mtimes = mt
            if changed and self._on_reload:
                self._on_reload(cfg)
            return cfg

    def force_reload(self) -> ResolvedConfig:
        with self._lock:
            self._mtimes = {}
        return self.snapshot()

    def describe(self) -> dict[str, Any]:
        cfg = self.snapshot()
        return cfg.summary()

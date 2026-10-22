"""Tùy chọn toàn cục, gom vào một object truyền qua ctx.obj."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GlobalOpts:
    repo: Path | None = None
    home: Path | None = None
    config_dir: Path | None = None
    provider: str | None = None  # override alias → provider mock|anthropic|openai|local|cli|codex
    verbose: int = 0
    flags: tuple[str, ...] = field(default_factory=tuple)  # --minimal --no-learn …

    def resolved_home(self) -> Path:
        if self.home:
            return self.home
        if self.repo:
            r = self.repo.resolve()
            return r.parent / f"{r.name}.ddt"
        return Path.cwd() / ".ddt"

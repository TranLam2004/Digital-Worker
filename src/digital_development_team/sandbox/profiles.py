"""SandboxProfile từ policy.yaml#sandbox.profiles — thay $WORKTREE, giữ cmd_allowlist cho profile scan."""

from __future__ import annotations

from pathlib import Path

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.errors import SandboxError
from digital_development_team.core.models import SandboxProfile

PROFILE_FOR_VARIANT = {None: "dev", "readonly": "verifier", "scan": "scan"}
"""exec.run → dev · exec.run:readonly → verifier · exec.run:scan → scan."""


def profile_for(cfg: SandboxCfg, name: str, worktree: Path | None) -> SandboxProfile:
    raw = cfg.profiles.get(name)
    if raw is None:
        raise SandboxError(f"sandbox profile lạ: {name!r}", hint=f"có: {sorted(cfg.profiles)}")
    writable = tuple(
        (str(worktree) if worktree else "$WORKTREE") if w == "$WORKTREE" else w for w in raw.writable
    )
    if name not in ("dev", "verifier", "scan"):
        raise SandboxError(f"profile {name!r} không phải dev|verifier|scan")
    return SandboxProfile(
        name=name,
        writable=writable,
        cmd_allowlist=tuple(raw.cmd_allowlist) if raw.cmd_allowlist else None,
        network=cfg.network != "none",
    )

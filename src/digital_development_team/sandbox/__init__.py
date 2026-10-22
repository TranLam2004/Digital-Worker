"""Sandbox — chạy lệnh cho exec.run / test.run.

LocalSandbox (P1): process con qua util.proc, env allowlist, cwd = worktree. KHÔNG phải ranh giới bảo mật thật
(policy deny-list + tool allowlist là lớp chặn chính). DockerSandbox: TODO(P2) — cùng Protocol Sandbox.

    sb = build_sandbox(cfg.policy.sandbox, scratch=home/state/tmp)
    res = sb.run("pytest -q", cwd=worktree, profile=profile_for(cfg, "dev", worktree), timeout_s=120, cancel=cancel)
"""

from __future__ import annotations

from pathlib import Path

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.errors import SandboxError
from digital_development_team.core.protocols import Sandbox
from digital_development_team.sandbox.local import LocalSandbox
from digital_development_team.sandbox.profiles import profile_for

__all__ = ["LocalSandbox", "build_sandbox", "profile_for"]


def build_sandbox(cfg: SandboxCfg, *, scratch: Path) -> Sandbox:
    if cfg.kind == "local":
        return LocalSandbox(scratch=scratch, default_timeout_s=float(cfg.timeout_sec))
    if cfg.kind == "docker":
        from digital_development_team.sandbox.docker import DockerSandbox

        return DockerSandbox(cfg)
    raise SandboxError(f"sandbox.kind lạ: {cfg.kind}")  # pragma: no cover - Literal đã chặn

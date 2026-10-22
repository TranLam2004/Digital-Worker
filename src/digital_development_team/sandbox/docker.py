"""DockerSandbox — TODO(P2): container dùng chung `ddt-runner:<lang>`, mount worktree rw/ro, network none, cpu/mem cap.

Giữ Protocol để runner/tool không đổi khi bật `sandbox.kind: docker`.
"""

from __future__ import annotations

from pathlib import Path

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import SandboxError
from digital_development_team.core.models import ExecResult, SandboxProfile


class DockerSandbox:
    name = "docker"

    def __init__(self, cfg: SandboxCfg) -> None:
        self._cfg = cfg

    def run(
        self,
        cmd: str,
        *,
        cwd: Path,
        profile: SandboxProfile,
        timeout_s: float,
        stdin: str | None = None,
        cancel: CancelToken,
    ) -> ExecResult:
        raise SandboxError(
            "DockerSandbox chưa triển khai (TODO P2)",
            hint="đặt policy.sandbox.kind: local, hoặc cài Docker và chờ bước P2",
            image=self._cfg.image,
        )

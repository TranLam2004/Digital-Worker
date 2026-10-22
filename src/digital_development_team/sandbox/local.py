"""LocalSandbox — process con trên máy host. Dùng bash (Git Bash trên Windows) nếu có, không thì cmd.exe.

Không cô lập thật (fs/network/cpu). An toàn dựa vào: policy deny-list exec, tool allowlist theo role, cwd = worktree,
env allowlist, timeout + kill cây. Profile readonly/scan: exec tool kiểm thêm (snapshot mtime · cmd allowlist).
"""

from __future__ import annotations

import contextlib
import os
import sys
import uuid
from pathlib import Path

from digital_development_team.core.context import CancelToken
from digital_development_team.core.models import ExecResult, SandboxProfile
from digital_development_team.sandbox.env import build_env
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.proc import run_process, which_bash

_IS_WIN = sys.platform == "win32"


class LocalSandbox:
    name = "local"

    def __init__(self, *, scratch: Path, default_timeout_s: float = 120.0) -> None:
        self._scratch = scratch
        self._default_timeout = default_timeout_s
        self._bash = which_bash()

    @property
    def shell(self) -> str:
        return "bash" if self._bash else ("cmd" if _IS_WIN else "sh")

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
        token = uuid.uuid4().hex[:8]
        self._scratch.mkdir(parents=True, exist_ok=True)
        out, err = self._scratch / f"exec-{token}.out", self._scratch / f"exec-{token}.err"
        worktree = str(cwd)
        if self._bash:
            script = self._scratch / f"cmd-{token}.sh"
            write_text_atomic(script, "set -o pipefail\n" + cmd + "\n", fsync=False)
            argv = [self._bash, "--noprofile", "--norc", _bash_path(script)]
        elif _IS_WIN:
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", cmd]
        else:  # pragma: no cover - không Windows và không bash
            argv = ["/bin/sh", "-c", cmd]
        env = build_env(worktree=worktree, extra={"DDT_SANDBOX_PROFILE": profile.name})
        try:
            return run_process(
                argv,
                cwd=cwd,
                env=env,
                timeout_s=timeout_s
                if timeout_s
                else self._default_timeout,  # caller (tool/budgets) quyết; default khi không truyền
                stdout_path=out,
                stderr_path=err,
                cancel=cancel,
                stdin_text=stdin,
            )
        finally:
            if self._bash:
                with contextlib.suppress(OSError):
                    (self._scratch / f"cmd-{token}.sh").unlink(missing_ok=True)

    def read_output(self, res: ExecResult) -> tuple[str, str]:
        def _read(p: str) -> str:
            path = Path(p)
            return path.read_text(encoding=ENCODING, errors="replace") if path.is_file() else ""

        return _read(res.stdout_path), _read(res.stderr_path)


def _bash_path(p: Path) -> str:
    """C:\\x\\y.sh → /c/x/y.sh cho Git Bash."""
    s = str(p)
    if _IS_WIN and len(s) > 2 and s[1] == ":":
        return "/" + s[0].lower() + s[2:].replace("\\", "/")
    return s

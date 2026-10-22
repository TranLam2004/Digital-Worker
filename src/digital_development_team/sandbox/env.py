"""Env cho process trong sandbox: allowlist host env + ép UTF-8 + tắt màu/prompt. KHÔNG truyền .env host."""

from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping

_PASS = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "LANG",
    "LC_ALL",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "PYTHONPATH",
    "JAVA_HOME",
    "GOPATH",
    "GOROOT",
    "CARGO_HOME",
    "NVM_DIR",
    # Windows: MSBuild/.NET/Node/JVM cần các biến này để tìm SDK và chạy được
    "SYSTEMDRIVE",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "USERNAME",
    "USERDOMAIN",
    "DOTNET_ROOT",
    "DOTNET_CLI_HOME",
    "NUGET_PACKAGES",
    "GRADLE_USER_HOME",
    "MAVEN_HOME",
    "M2_HOME",
    "GOCACHE",
    "GOMODCACHE",
    "NVM_HOME",
    "NVM_SYMLINK",
)
_FORCE = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PY_COLORS": "0",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "TERM": "dumb",
    "CI": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}
_NEVER_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def build_env(
    *,
    worktree: str | None = None,
    extra: Mapping[str, str] | None = None,
    host: Mapping[str, str] | None = None,
) -> dict[str, str]:
    src = host if host is not None else os.environ
    env = {
        k: v
        for k, v in src.items()
        if k.upper() in _PASS and not any(s in k.upper() for s in _NEVER_SUBSTRINGS)
    }
    env.update(_FORCE)
    if worktree:
        env["WORKTREE"] = worktree
        env["DDT_WORKTREE"] = worktree
        site = pathlib.Path(worktree) / ".ddt" / "site"
        if site.is_dir():  # gói pkg.install cài cho riêng worktree (pip --target)
            env["PYTHONPATH"] = str(site) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if extra:
        for k, v in extra.items():
            if any(s in k.upper() for s in _NEVER_SUBSTRINGS):
                continue
            env[k] = v
    return env

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from digital_development_team.core.context import CancelToken
from digital_development_team.util import proc

PY = sys.executable


def _env() -> dict[str, str]:
    base = {
        k: v
        for k, v in os.environ.items()
        if k in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP")
    }
    base["PYTHONUTF8"] = "1"
    return base


def test_run_process_ok(tmp_path: Path) -> None:
    r = proc.run_process(
        [PY, "-c", "print('xin chào'); import sys; print('err', file=sys.stderr)"],
        cwd=tmp_path,
        env=_env(),
        timeout_s=30,
        stdout_path=tmp_path / "out.txt",
        stderr_path=tmp_path / "err.txt",
        cancel=CancelToken(),
    )
    assert r.exit_code == 0
    assert r.stdout_tail == ("xin chào",)
    assert r.stderr_tail == ("err",)
    assert not r.timed_out and not r.cancelled


def test_run_process_nonzero_is_info_not_error(tmp_path: Path) -> None:
    r = proc.run_process(
        [PY, "-c", "import sys; sys.exit(3)"],
        cwd=tmp_path,
        env=_env(),
        timeout_s=30,
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel=CancelToken(),
    )
    assert r.exit_code == 3


@pytest.mark.slow
def test_run_process_timeout_kills_tree(tmp_path: Path) -> None:
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
    r = proc.run_process(
        [PY, "-c", code],
        cwd=tmp_path,
        env=_env(),
        timeout_s=1.0,
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel=CancelToken(),
    )
    assert r.timed_out
    assert r.duration_ms < 10_000


def test_run_process_cancel(tmp_path: Path) -> None:
    tok = CancelToken()
    tok.cancel("test")
    r = proc.run_process(
        [PY, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=_env(),
        timeout_s=30,
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel=tok,
    )
    assert r.cancelled


def test_run_process_stdin(tmp_path: Path) -> None:
    r = proc.run_process(
        [PY, "-c", "import sys; print(sys.stdin.read().upper())"],
        cwd=tmp_path,
        env=_env(),
        timeout_s=30,
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel=CancelToken(),
        stdin_text="abc",
    )
    assert r.stdout_tail == ("ABC",)


def test_which_bash_not_wsl() -> None:
    b = proc.which_bash()
    if b is not None:
        assert "system32" not in b.lower()

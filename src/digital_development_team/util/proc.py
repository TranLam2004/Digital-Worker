"""Nơi DUY NHẤT gọi subprocess. Ruff cấm import subprocess ở mọi nơi khác (trừ isolation.git).

Thiết kế cho Windows:
- stdout/stderr ra FILE, không PIPE → không deadlock, tiện artifactize.
- stdin=DEVNULL → git/pytest không chờ input.
- CREATE_NEW_PROCESS_GROUP + kill cây bằng psutil (Job Object nếu có pywin32).
- Poll 50ms để kiểm CancelToken và deadline.
- encoding utf-8, PYTHONUTF8=1 cho con.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import psutil

from digital_development_team.core.context import CancelToken
from digital_development_team.core.models import ExecResult
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.text import tail

log = logging.getLogger(__name__)

_POLL_S = 0.05
_TAIL_LINES = 10

_IS_WIN = sys.platform == "win32"

try:  # pywin32 — Job Object để kill chắc chắn cả cây
    if _IS_WIN:
        import win32job

        _HAS_JOB = True
    else:
        _HAS_JOB = False
except ImportError:  # pragma: no cover
    _HAS_JOB = False


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    stdout_path: Path,
    stderr_path: Path,
    cancel: CancelToken,
    stdin_text: str | None = None,
) -> ExecResult:
    """Chạy argv, output ra file. Timeout/cancel → kill cây → ExecResult(timed_out|cancelled)."""
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    deadline = start + timeout_s

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WIN else 0
    stdin_file = None
    if stdin_text is not None:
        stdin_file = stdout_path.with_suffix(".stdin")
        stdin_file.write_text(stdin_text, encoding=ENCODING)

    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        stdin_handle = stdin_file.open("rb") if stdin_file else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=dict(env),
                stdin=stdin_handle,
                stdout=out,
                stderr=err,
                creationflags=creationflags,
                start_new_session=not _IS_WIN,
            )
        finally:
            if stdin_file:
                stdin_handle.close()  # type: ignore[union-attr]

        job = _assign_job(proc.pid) if _HAS_JOB else None
        timed_out = cancelled = False
        try:
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                now = time.monotonic()
                if cancel.is_cancelled(mono_now=now):
                    cancelled = True
                    break
                if now >= deadline:
                    timed_out = True
                    break
                time.sleep(_POLL_S)
            if timed_out or cancelled:
                _kill_tree(proc.pid, job)
                with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover
                    proc.wait(timeout=5)
        finally:
            if job is not None:
                with contextlib.suppress(Exception):  # pragma: no cover
                    win32job.CloseHandle(job)

    duration_ms = int((time.monotonic() - start) * 1000)
    out_lines = _read_lines(stdout_path)
    err_lines = _read_lines(stderr_path)
    exit_code = proc.returncode if proc.returncode is not None else -1
    return ExecResult(
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        stdout_tail=tail(out_lines, _TAIL_LINES),
        stderr_tail=tail(err_lines, _TAIL_LINES),
        stdout_lines=len(out_lines),
        stderr_lines=len(err_lines),
        duration_ms=duration_ms,
        timed_out=timed_out,
        cancelled=cancelled,
    )


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding=ENCODING, errors="replace").splitlines()
    except OSError:
        return []


def _assign_job(pid: int) -> object | None:
    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        import win32api
        import win32con

        handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
        win32job.AssignProcessToJobObject(job, handle)
        return cast("object", job)
    except Exception as e:
        log.debug("không gán Job Object cho pid %s: %s", pid, e)
        return None


def _kill_tree(pid: int, job: object | None) -> None:
    if job is not None:
        try:
            win32job.TerminateJobObject(job, 1)
            return
        except Exception as e:  # rơi về psutil
            log.debug("TerminateJobObject thất bại, dùng psutil: %s", e)
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for p in (*children, parent):
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()
    psutil.wait_procs((*children, parent), timeout=3)


def which_bash() -> str | None:
    """Git Bash. Loại C:\\Windows\\System32\\bash.exe (WSL)."""
    import shutil

    if not _IS_WIN:
        return shutil.which("bash")
    candidates = [
        os.environ.get("DDT_BASH"),
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    for c in candidates:
        if c and Path(c).exists() and "system32" not in c.lower():
            return c
    return None


def stream_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    stderr_path: Path,
    cancel: CancelToken,
    on_line: Callable[[str], None],
    stop: Callable[[], bool] | None = None,
    stdin_text: str | None = None,
) -> ExecResult:
    """Như run_process nhưng stdout đọc TỪNG DÒNG (thread) → on_line; stop() True → kill cây (budget).

    Dùng cho transport CLI (stream-json). stderr vẫn ra file. cancelled=True khi stop() hoặc cancel.
    """
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    deadline = start + timeout_s
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WIN else 0
    lines_seen = 0
    with stderr_path.open("wb") as err:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            creationflags=creationflags,
            start_new_session=not _IS_WIN,
            text=True,
            encoding=ENCODING,
            errors="replace",
            bufsize=1,
        )
        if stdin_text is not None and proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.write(stdin_text)
                proc.stdin.close()
        job = _assign_job(proc.pid) if _HAS_JOB else None
        done = threading.Event()

        def _reader() -> None:
            nonlocal lines_seen
            assert proc.stdout is not None  # noqa: S101
            try:
                for line in proc.stdout:
                    lines_seen += 1
                    try:
                        on_line(line.rstrip("\r\n"))
                    except Exception:
                        log.exception("stream_process on_line lỗi")
            finally:
                done.set()

        threading.Thread(target=_reader, name="ddt-stream", daemon=True).start()
        timed_out = cancelled = False
        try:
            while True:
                if proc.poll() is not None and done.is_set():
                    break
                now = time.monotonic()
                if cancel.is_cancelled(mono_now=now) or (stop is not None and stop()):
                    cancelled = True
                    break
                if now >= deadline:
                    timed_out = True
                    break
                time.sleep(_POLL_S)
            if timed_out or cancelled:
                _kill_tree(proc.pid, job)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            done.wait(timeout=2)
        finally:
            if job is not None:
                with contextlib.suppress(Exception):  # pragma: no cover
                    win32job.CloseHandle(job)
    duration_ms = int((time.monotonic() - start) * 1000)
    err_lines = (
        stderr_path.read_text(encoding=ENCODING, errors="replace").splitlines()
        if stderr_path.exists()
        else []
    )
    return ExecResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout_path="",
        stderr_path=str(stderr_path),
        stdout_tail=(),
        stderr_tail=tuple(tail(err_lines, _TAIL_LINES)),
        stdout_lines=lines_seen,
        stderr_lines=len(err_lines),
        duration_ms=duration_ms,
        timed_out=timed_out,
        cancelled=cancelled,
    )


def spawn_detached(
    argv: list[str], *, cwd: Path, log_path: Path, env: Mapping[str, str] | None = None
) -> int:
    """Khởi động process sống tách khỏi process gọi (vd watchdog bật lại `ddt worker`). stdout+stderr → log_path. Trả pid.

    Không chờ, không giết khi process gọi thoát. Chỉ dùng cho lệnh do harness tự dựng argv (không qua shell).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if _IS_WIN:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    with log_path.open("ab") as log:
        proc = subprocess.Popen(  # argv cố định do harness dựng, không qua shell
            argv,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=dict(env) if env is not None else None,
            creationflags=flags,
            start_new_session=not _IS_WIN,
        )
    return int(proc.pid)


class PipeProcess:
    """Process sống lâu nói chuyện theo dòng qua stdin/stdout (MCP stdio). stderr → file log. close() giết cả cây.

    argv do harness dựng từ cấu hình đã duyệt (không qua shell). Một thread đọc stdout → hàng đợi để recv có timeout.
    """

    def __init__(self, argv: list[str], *, cwd: Path, env: Mapping[str, str], stderr_path: Path) -> None:
        import queue

        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._err = stderr_path.open("ab")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if _IS_WIN else 0
        self._proc = subprocess.Popen(  # argv cố định từ policy.yaml#mcp_client, không qua shell
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._err,
            env=dict(env),
            creationflags=flags,
            start_new_session=not _IS_WIN,
        )
        self._job = _assign_job(self._proc.pid) if _HAS_JOB else None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        out = self._proc.stdout
        assert out is not None  # noqa: S101 - stdout=PIPE
        for raw in iter(out.readline, b""):
            self._lines.put(raw.decode(ENCODING, errors="replace").rstrip("\r\n"))
        self._lines.put(None)

    @property
    def pid(self) -> int:
        return int(self._proc.pid)

    def alive(self) -> bool:
        return self._proc.poll() is None

    def send(self, line: str) -> None:
        stdin = self._proc.stdin
        if stdin is None or not self.alive():
            raise EOFError("process đã thoát")
        stdin.write((line + "\n").encode(ENCODING))
        stdin.flush()

    def recv(self, timeout: float) -> str | None:
        """Một dòng stdout; None khi hết giờ. EOFError khi process đã đóng stdout."""
        import queue

        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            self._lines.put(None)
            raise EOFError("process đã đóng stdout")
        return line

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        if self.alive():
            _kill_tree(self._proc.pid, self._job)
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=3)
        self._err.close()

"""Ghi/đọc file an toàn. Mọi text là UTF-8 (máy mặc định cp1252 — không được dựa vào default)."""

from __future__ import annotations

import contextlib
import os
import sys
import time
import uuid
from pathlib import Path

ENCODING = "utf-8"
_REPLACE_RETRIES = 10
_REPLACE_BACKOFF_S = 0.05


def read_text(path: Path) -> str:
    for attempt in range(_REPLACE_RETRIES):
        try:
            return path.read_text(encoding=ENCODING)
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
    raise OSError(f"read failed: {path}")  # unreachable for a positive retry count


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def write_text_atomic(path: Path, text: str, *, fsync: bool = True) -> None:
    """tmp cùng thư mục → fsync → os.replace (retry trên Windows khi file đích đang mở)."""
    write_bytes_atomic(path, text.encode(ENCODING), fsync=fsync)


def write_bytes_atomic(path: Path, data: bytes, *, fsync: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
        if fsync and sys.platform != "win32":
            _fsync_dir(path.parent)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _replace_with_retry(src: Path, dst: Path) -> None:
    last: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            Path(src).replace(dst)
            return
        except PermissionError as e:  # Windows: file đích đang bị mở (AV, viewer)
            last = e
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
    raise last if last is not None else OSError(f"replace thất bại: {dst}")


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def detect_eol(text: str) -> str:
    """'\\r\\n' nếu đa số dòng CRLF, ngược lại '\\n'."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def normalize_eol(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def apply_eol(text_lf: str, eol: str) -> str:
    return text_lf if eol == "\n" else text_lf.replace("\n", eol)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

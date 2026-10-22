from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from digital_development_team.util.jsonio import dumps_canonical

PREFIX = "sha256:"


def sha256_bytes(data: bytes) -> str:
    return PREFIX + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(data: Any) -> str:
    return sha256_text(dumps_canonical(data))


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return PREFIX + h.hexdigest()


def short(digest: str, n: int = 8) -> str:
    return digest.removeprefix(PREFIX)[:n]

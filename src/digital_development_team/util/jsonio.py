from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from digital_development_team.util.fsio import read_text, write_text_atomic


def dumps(data: Any, *, indent: int | None = 2) -> str:
    """UTF-8, không escape unicode, dòng kết \\n."""
    s = json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=False)
    return s + "\n" if indent is not None else s


def dumps_canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> Any:
    return json.loads(text)


def load_file(path: Path) -> Any:
    return json.loads(read_text(path))


def save_file(path: Path, data: Any, *, fsync: bool = True) -> None:
    write_text_atomic(path, dumps(data), fsync=fsync)

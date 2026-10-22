"""Tên tool canonical (`fs.read`) ↔ wire (`fs_read`). API chỉ nhận ^[a-zA-Z0-9_-]{1,64}$."""

from __future__ import annotations

import re

from digital_development_team.core.errors import ConfigError

WIRE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
CANONICAL_RE = re.compile(r"^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*$")


def to_wire(canonical: str) -> str:
    if not CANONICAL_RE.match(canonical):
        raise ConfigError(f"tên tool canonical sai: {canonical!r}", hint="dạng namespace.op, vd fs.read")
    wire = canonical.replace(".", "_")
    if not WIRE_RE.match(wire):  # pragma: no cover - CANONICAL_RE đã đảm bảo
        raise ConfigError(f"tên wire không hợp lệ: {wire!r}")
    return wire


def to_canonical(wire: str) -> str:
    """fs_read → fs.read. Chỉ tách ở dấu _ đầu tiên: blackboard_write → blackboard.write, llm_ask → llm.ask."""
    ns, sep, op = wire.partition("_")
    if not sep or not op:
        raise ConfigError(f"tên wire không có namespace: {wire!r}")
    return f"{ns}.{op}"

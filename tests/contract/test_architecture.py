"""Luật layer: package chỉ được import package ở layer thấp hơn hoặc bằng (trừ chính nó).

Không cần import-linter. Scan ast mọi file trong src.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import SRC

PKG = "digital_development_team"

LAYERS: dict[str, int] = {
    "core": 0,
    "util": 1,
    "expr": 1,
    "config": 2,
    "eventlog": 2,
    "storage": 2,
    "schema": 3,
    "blackboard": 4,
    "llm": 4,
    "sandbox": 4,
    "policy": 4,
    "budget": 4,
    "risk": 4,
    "isolation": 4,
    "condenser": 4,
    "memory": 4,
    "checklist": 4,
    "gates": 4,
    "gamemaster": 4,
    "tools": 5,
    "ingest": 5,
    "agents": 6,
    "runner": 7,
    "onboarding": 7,
    "reporting": 7,
    "app": 8,
    "logging_setup": 8,
    "cli": 9,
}

FORBIDDEN: dict[str, set[str]] = {}  # cấm riêng ngoài luật layer (đang trống)


def _top_package(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != PKG or len(parts) < 2:
        return None
    return parts[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _py_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "_assets" not in p.parts]


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_imports_go_downward(path: Path) -> None:
    rel = path.relative_to(SRC)
    own = rel.parts[0] if len(rel.parts) > 1 else rel.stem
    if own in ("__init__", "__main__"):
        own = "cli"
    assert own in LAYERS, f"package {own!r} chưa khai báo trong LAYERS"
    own_layer = LAYERS[own]
    for mod in _imports(path):
        dep = _top_package(mod)
        if dep is None or dep == own:
            continue
        assert dep in LAYERS, f"{rel}: import package lạ {dep!r}"
        assert LAYERS[dep] <= own_layer, f"{rel}: {own}(L{own_layer}) import {dep}(L{LAYERS[dep]}) — đi lên"
        assert dep not in FORBIDDEN.get(own, set()), f"{rel}: {own} bị cấm import {dep}"


def test_core_imports_nothing_internal() -> None:
    for path in (SRC / "core").glob("*.py"):
        for mod in _imports(path):
            dep = _top_package(mod)
            assert dep in (None, "core"), f"core/{path.name} import {mod}"


def test_no_subprocess_outside_proc() -> None:
    allowed = {SRC / "util" / "proc.py", SRC / "isolation" / "git.py"}
    for path in _py_files():
        if path in allowed:
            continue
        imports = _imports(path)
        assert "subprocess" not in imports, f"{path.relative_to(SRC)} import subprocess — dùng util.proc"

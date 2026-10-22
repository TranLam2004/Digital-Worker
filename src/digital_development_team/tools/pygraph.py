"""PyGraph — backend code-intelligence thuần Python (stdlib `ast`) cho `codegraph.*` khi không có CodeGraph CLI.

Đóng gói sẵn, 0 cài thêm: index mọi file .py dưới root (bỏ .git/.venv/node_modules/__pycache__/.codegraph), lấy
function · class · method (span dòng, signature), cạnh `calls` theo tên (Name / Attribute.attr), `imports`.
Độ chính xác: theo tên, không theo kiểu (như CodeGraph tree-sitter) — đủ cho callers/impact/affected_tests.
Cache theo (root, chữ ký mtime+size của các file .py); repo vài nghìn file index trong ~1–3 s.
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.testpaths import is_test_path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".codegraph",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
    ".eggs",
}


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str  # function | method | class
    path: str  # posix, tương đối root
    line: int
    end_line: int
    qualified: str  # Class::method | name
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "signature": self.signature,
            "qualified_name": self.qualified,
        }


@dataclass
class _FileIndex:
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (caller qualified, callee name)
    imports: set[str] = field(default_factory=set)  # module names imported


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, idx: _FileIndex) -> None:
        self._path = path
        self._idx = idx
        self._stack: list[str] = []  # qualified names of enclosing defs/classes

    def _span(self, n: ast.AST) -> tuple[int, int]:
        return int(getattr(n, "lineno", 0)), int(getattr(n, "end_lineno", getattr(n, "lineno", 0)))

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._idx.imports.add(a.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._idx.imports.add(node.module.split(".")[0])
        for a in node.names:
            self._idx.imports.add(a.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        line, end = self._span(node)
        self._idx.symbols.append(Symbol(node.name, "class", self._path, line, end, node.name))
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def _def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        line, end = self._span(node)
        in_class = bool(self._stack) and self._is_class(self._stack[-1])
        kind = "method" if in_class else "function"
        qualified = f"{self._stack[-1]}::{node.name}" if in_class else node.name
        sig = f"({', '.join(a.arg for a in node.args.args)})"
        self._idx.symbols.append(Symbol(node.name, kind, self._path, line, end, qualified, sig))
        self._stack.append(qualified)
        self.generic_visit(node)
        self._stack.pop()

    def _is_class(self, qualified: str) -> bool:
        return any(s.qualified == qualified and s.kind == "class" for s in self._idx.symbols)

    visit_FunctionDef = _def
    visit_AsyncFunctionDef = _def

    def visit_Call(self, node: ast.Call) -> None:
        callee = None
        f = node.func
        if isinstance(f, ast.Name):
            callee = f.id
        elif isinstance(f, ast.Attribute):
            callee = f.attr
        if callee:
            caller = self._stack[-1] if self._stack else f"<module:{self._path}>"
            self._idx.calls.append((caller, callee))
        self.generic_visit(node)


class PyGraph:
    """Index theo root; `refresh()` khi chữ ký file đổi. Mọi truy vấn O(n symbols) — đủ nhanh cho repo trung bình."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._sig: tuple[tuple[str, int, int], ...] = ()
        self._files: dict[str, _FileIndex] = {}
        self._by_name: dict[str, list[Symbol]] = defaultdict(list)
        self._callers: dict[str, set[str]] = defaultdict(set)  # callee name → caller qualified (with path)
        self._callees: dict[str, set[str]] = defaultdict(set)  # caller qualified(with path) → callee names
        self.refresh()

    # ───── index ─────
    def _py_files(self) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield Path(dirpath) / fn

    def _signature(self, files: list[Path]) -> tuple[tuple[str, int, int], ...]:
        out = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((str(p), int(st.st_mtime_ns), int(st.st_size)))
        return tuple(out)

    def refresh(self) -> bool:
        files = sorted(self._py_files())
        sig = self._signature(files)
        if sig == self._sig:
            return False
        self._sig = sig
        self._files.clear()
        self._by_name.clear()
        self._callers.clear()
        self._callees.clear()
        for p in files:
            rel = p.relative_to(self.root).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding=ENCODING, errors="replace"))
            except (SyntaxError, ValueError):
                continue
            idx = _FileIndex()
            _Visitor(rel, idx).visit(tree)
            self._files[rel] = idx
            for s in idx.symbols:
                self._by_name[s.name].append(s)
            for caller, callee in idx.calls:
                key = f"{rel}::{caller}"
                self._callers[callee].add(key)
                self._callees[key].add(callee)
        return True

    @property
    def stats(self) -> dict[str, int]:
        return {
            "files": len(self._files),
            "symbols": sum(len(f.symbols) for f in self._files.values()),
            "calls": sum(len(f.calls) for f in self._files.values()),
        }

    # ───── queries ─────
    def search(self, query: str, *, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        q = query.lower()
        hits: list[tuple[float, Symbol]] = []
        for name, syms in self._by_name.items():
            ln = name.lower()
            if q == ln:
                score = 100.0
            elif ln.startswith(q):
                score = 80.0
            elif q in ln:
                score = 60.0
            else:
                continue
            for s in syms:
                if kind and s.kind != kind:
                    continue
                hits.append((score - (5.0 if is_test_path(s.path) else 0.0), s))
        hits.sort(key=lambda t: (-t[0], t[1].path, t[1].line))
        return [{**s.to_dict(), "score": sc} for sc, s in hits[:limit]]

    def exact(self, name: str, path: str | None = None) -> list[Symbol]:
        syms = self._by_name.get(name, [])
        if path:
            want = path.replace("\\", "/")
            syms = [s for s in syms if s.path.endswith(want)]
        return sorted(syms, key=lambda s: (s.path, s.line))

    def _resolve_key(self, key: str) -> Symbol | None:
        """'path::Class::method' | 'path::func' | 'path::<module:…>' → Symbol (None cho module-level)."""
        path, _, qualified = key.partition("::")
        for s in self._files.get(path, _FileIndex()).symbols:
            if s.qualified == qualified:
                return s
        return None

    def callers(self, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key in sorted(self._callers.get(name, ())):
            s = self._resolve_key(key)
            if s is not None:
                out.append(s.to_dict())
            else:
                path = key.split("::", 1)[0]
                out.append(
                    {
                        "name": "<module>",
                        "kind": "module",
                        "path": path,
                        "line": 1,
                        "end_line": 1,
                        "signature": None,
                        "qualified_name": f"{path}::<module>",
                    }
                )
        return out[:limit]

    def callees(self, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
        names: set[str] = set()
        for s in self._by_name.get(name, []):
            names |= self._callees.get(f"{s.path}::{s.qualified}", set())
        out: list[dict[str, Any]] = []
        for n in sorted(names):
            for s in self._by_name.get(n, [])[:1]:
                out.append(s.to_dict())
        return out[:limit]

    def impact(self, name: str, *, depth: int = 2, max_nodes: int = 200) -> dict[str, Any]:
        """Reverse BFS trên callers theo tên, depth N → {affected[], files[], tests[], depth_reached, truncated}."""
        seen: set[str] = set()
        frontier = [name]
        affected: list[Symbol] = []
        depth_reached = 0
        truncated = False
        for d in range(1, depth + 1):
            nxt: list[str] = []
            for n in frontier:
                for key in self._callers.get(n, ()):
                    if key in seen:
                        continue
                    seen.add(key)
                    s = self._resolve_key(key)
                    if s is None:
                        path = key.split("::", 1)[0]
                        s = Symbol("<module>", "module", path, 1, 1, f"{path}::<module>")
                    affected.append(s)
                    depth_reached = d
                    nxt.append(s.name)
                    if len(affected) >= max_nodes:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated or not nxt:
                break
            frontier = nxt
        files = sorted({s.path for s in affected})
        return {
            "affected": [s.to_dict() for s in affected],
            "files": files,
            "tests": sorted(f for f in files if is_test_path(f)),
            "depth_reached": depth_reached,
            "truncated": truncated,
        }

    def source(self, s: Symbol, *, max_lines: int = 120) -> str | None:
        p = self.root / s.path
        if not p.is_file():
            return None
        lines = p.read_text(encoding=ENCODING, errors="replace").splitlines()
        return "\n".join(lines[s.line - 1 : min(s.end_line, s.line - 1 + max_lines)])


_CACHE: dict[str, PyGraph] = {}


def pygraph_for(root: Path) -> PyGraph:
    """Một PyGraph mỗi root trong process; refresh theo chữ ký file mỗi lần lấy."""
    key = str(root.resolve())
    g = _CACHE.get(key)
    if g is None:
        g = _CACHE[key] = PyGraph(root)
    else:
        g.refresh()
    return g

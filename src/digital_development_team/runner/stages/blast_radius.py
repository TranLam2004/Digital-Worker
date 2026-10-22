"""Blast radius của PATCH (0 LLM, chạy trong MACHINE_GATE): diff → symbol đổi (ast, Python) → CodeGraph impact
(reverse BFS depth N, repo chính) → gộp file/test ảnh hưởng → FMEA RPN (risk/blast.py).

Ý tưởng mượn Honda POC: quy tắc "method-not-class" (class chỉ tính là đổi khi có dòng đổi NGOÀI mọi method), chain
ngắn, affected_tests để Verifier đọc trước. Không port code. Không có index/binary → status skipped, pipeline đi tiếp.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from digital_development_team.risk.blast import fmea
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.testpaths import is_test_path

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)

ImpactFn = Callable[[str], dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class ChangedSymbol:
    symbol: str
    kind: str  # function | method | class
    file: str
    line_start: int
    line_end: int
    qualified: str


def hunk_new_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Unified diff → {path: [(start, end)]} theo số dòng phía MỚI (+). Hunk xóa thuần (+n,0) vẫn ghi (n, n)."""
    out: dict[str, list[tuple[int, int]]] = {}
    cur: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            m = _FILE_RE.match(line)
            cur = m.group(1).strip() if m else None
            if cur:
                out.setdefault(cur, [])
            continue
        if cur and line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                out[cur].append((start, max(start, start + count - 1)))
    return out


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def changed_symbols_py(path: str, source: str, ranges: Sequence[tuple[int, int]]) -> list[ChangedSymbol]:
    """Symbol Python đổi: function/method chứa dòng đổi; class chỉ khi dòng đổi nằm ngoài mọi method của nó."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[ChangedSymbol] = []

    def span(n: ast.AST) -> tuple[int, int]:
        return (getattr(n, "lineno", 0), getattr(n, "end_lineno", getattr(n, "lineno", 0)))

    def hit(sp: tuple[int, int]) -> bool:
        return any(_overlaps(sp, r) for r in ranges)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if hit(span(node)):
                out.append(ChangedSymbol(node.name, "function", path, *span(node), node.name))
        elif isinstance(node, ast.ClassDef):
            cspan = span(node)
            methods = [m for m in node.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)]
            method_spans = [span(m) for m in methods]
            for m, ms in zip(methods, method_spans, strict=True):
                if hit(ms):
                    out.append(ChangedSymbol(m.name, "method", path, *ms, f"{node.name}::{m.name}"))
            outside = any(
                _overlaps(cspan, r) and not any(_overlaps(ms, r) for ms in method_spans) for r in ranges
            )
            if outside:
                out.append(ChangedSymbol(node.name, "class", path, *cspan, node.name))
    return out


def changed_symbols(wt: Path, diff: str, files: Sequence[str]) -> tuple[list[ChangedSymbol], list[str]]:
    """→ (symbol đổi trong file .py, file bỏ qua vì không phải Python/không đọc được)."""
    ranges = hunk_new_ranges(diff)
    symbols: list[ChangedSymbol] = []
    skipped: list[str] = []
    for f in files:
        if not f.endswith(".py"):
            skipped.append(f)
            continue
        p = wt / f
        if not p.is_file():
            continue  # file xóa
        rs = ranges.get(f.replace("\\", "/")) or ranges.get(f) or []
        if not rs:
            continue
        symbols += changed_symbols_py(
            f.replace("\\", "/"), p.read_text(encoding=ENCODING, errors="replace"), rs
        )
    return symbols, skipped


def compute_blast_radius(
    wt: Path,
    diff: str,
    files: Sequence[str],
    impact_fn: ImpactFn | None,
    *,
    depth: int,
    coverage_diff_pct: float | None,
    touches_sensitive: bool,
    rpn_high: int,
    rpn_medium: int,
    max_symbols: int = 20,
) -> dict[str, Any]:
    """→ {status pass|skipped, changed[], changed_count, affected_files[], affected_tests[], affected_count, depth,
    truncated, fmea{...}, rpn, risk, summary, skipped_files[]}. impact_fn None = không có CodeGraph → skipped."""
    symbols, skipped = changed_symbols(wt, diff, files)
    changed = [asdict(s) for s in symbols]
    if impact_fn is None:
        return {
            "status": "skipped",
            "summary": "không có CodeGraph index — chỉ đếm symbol đổi",
            "changed": changed,
            "changed_count": len(changed),
            "skipped_files": skipped,
        }
    # symbol trong file test không lan ra production code → không gọi impact, không tính vào FMEA
    prod = [s for s in symbols if not is_test_path(s.file)]
    changed_files = {s.file for s in symbols}
    affected_files: set[str] = set()
    affected_tests: set[str] = set()
    public = False
    truncated = len(prod) > max_symbols
    for s in prod[:max_symbols]:
        r = impact_fn(s.symbol)
        if not r:
            continue
        affected_files |= {f for f in r.get("affected_files", []) if f not in changed_files}
        affected_tests |= set(r.get("affected_tests", []))
        public = public or (
            r.get("risk") == "high" and any(f.endswith("__init__.py") for f in r.get("affected_files", []))
        )
    src_affected = sorted(f for f in affected_files if f not in affected_tests)
    depth_reached = depth if (affected_files or affected_tests) else 0
    fm = fmea(
        affected_files=len(src_affected),
        depth_reached=depth_reached,
        changed_symbols=len(prod),
        coverage_changed_pct=coverage_diff_pct,
        touches_public_api=public,
        touches_sensitive=touches_sensitive,
        rpn_high=rpn_high,
        rpn_medium=rpn_medium,
    )
    summary = (
        f"{len(prod)} symbol đổi ({len(symbols) - len(prod)} trong test) → {len(src_affected)} file ảnh hưởng · "
        f"{len(affected_tests)} test · "
        f"RPN {fm.rpn} ({fm.risk})"
    )
    return {
        "status": "pass",
        "summary": summary,
        "changed": changed,
        "changed_count": len(changed),
        "affected_files": src_affected,
        "affected_tests": sorted(affected_tests),
        "affected_count": len(src_affected) + len(affected_tests),
        "depth": depth,
        "depth_reached": depth_reached,
        "truncated": truncated,
        "fmea": fm.to_dict(),
        "rpn": fm.rpn,
        "risk": fm.risk,
        "skipped_files": skipped,
    }


def as_scope_facts(blast: Mapping[str, Any]) -> dict[str, Any]:
    """Facts cho policy/brief: codegraph_impact_risk (tier_rules đã tham chiếu) + blast_radius đầy đủ."""
    facts: dict[str, Any] = {"blast_radius": dict(blast)}
    if blast.get("status") == "pass":
        facts["codegraph_impact_risk"] = blast["risk"]
    return facts

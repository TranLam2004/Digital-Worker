"""diff-cover `--format json:<path>`: {src_stats{<path>: {percent_covered, violation_lines[]}}, total_num_lines,
total_num_violations, total_percent_covered}. Chỉ tính dòng MỚI/đổi trong diff so với compare-branch."""

from __future__ import annotations

import json
from typing import Any


def parse_diff_cover(text: str, *, max_files: int = 50) -> dict[str, Any]:
    """→ {tool, diff_pct, new_lines, uncovered_count, uncovered_new_lines[{path, lines[]}], files[{path, pct}]}."""
    try:
        d = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        return {
            "tool": "diff-cover",
            "diff_pct": None,
            "new_lines": 0,
            "uncovered_count": 0,
            "uncovered_new_lines": [],
            "files": [],
            "errors": [f"parse_error: {e}"],
        }
    stats = d.get("src_stats") or {}
    uncovered: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for path, st in sorted(stats.items()):
        lines = [int(x) for x in (st.get("violation_lines") or [])]
        if len(files) < max_files:
            files.append({"path": path, "pct": _num(st.get("percent_covered"))})
        if lines:
            uncovered.append({"path": path, "lines": lines})
    total_lines = int(d.get("total_num_lines") or 0)
    pct = d.get("total_percent_covered")
    return {
        "tool": "diff-cover",
        "diff_pct": _num(pct) if total_lines else None,  # không có dòng mới → không có tỷ lệ
        "new_lines": total_lines,
        "uncovered_count": int(d.get("total_num_violations") or 0),
        "uncovered_new_lines": uncovered,
        "files": files,
    }


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 1) if v is not None else None
    except (TypeError, ValueError):
        return None

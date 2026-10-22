"""Cobertura coverage.xml (pytest-cov `--cov-report=xml`): <coverage line-rate=…> … <class filename=… line-rate=…>."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any


def parse_coverage_xml(text: str, *, max_files: int = 100) -> dict[str, Any]:
    """→ {tool, total_pct, lines_valid, lines_covered, files[{path, pct}]}. XML hỏng → total_pct None + errors."""
    try:
        root = ET.fromstring(text)  # noqa: S314 - xem ghi chú import
    except ET.ParseError as e:
        return {
            "tool": "coverage.py",
            "total_pct": None,
            "lines_valid": 0,
            "lines_covered": 0,
            "files": [],
            "errors": [f"parse_error: {e}"],
        }
    total = _rate(root.get("line-rate"))
    files: list[dict[str, Any]] = []
    for cls in root.iter("class"):
        if len(files) >= max_files:
            break
        files.append({"path": cls.get("filename", "?"), "pct": _rate(cls.get("line-rate"))})
    return {
        "tool": "coverage.py",
        "total_pct": total,
        "lines_valid": int(root.get("lines-valid") or 0),
        "lines_covered": int(root.get("lines-covered") or 0),
        "files": files,
    }


def _rate(v: str | None) -> float | None:
    try:
        return round(float(v) * 100, 1) if v is not None else None
    except ValueError:
        return None

"""Bandit `-f json`: results[{filename, line_number, issue_severity, issue_confidence, issue_text, test_id, issue_cwe{id}}]."""

from __future__ import annotations

import json
from typing import Any

_SEV = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def parse_bandit(text: str, *, max_findings: int = 20) -> dict[str, Any]:
    """→ {tool, high, medium, low, total, findings[], errors[]}. Text rỗng/không JSON → total 0 + parse_error."""
    try:
        d = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        return {
            "tool": "bandit",
            "high": 0,
            "medium": 0,
            "low": 0,
            "total": 0,
            "findings": [],
            "errors": [f"parse_error: {e}"],
        }
    results = d.get("results") or []
    counts = {"high": 0, "medium": 0, "low": 0}
    findings: list[dict[str, Any]] = []
    for r in results:
        sev = _SEV.get(str(r.get("issue_severity", "")).upper(), "info")
        if sev in counts:
            counts[sev] += 1
        cwe = (r.get("issue_cwe") or {}).get("id")
        if len(findings) < max_findings:
            findings.append(
                {
                    "severity": sev,
                    "cwe_or_category": f"CWE-{cwe}" if cwe else str(r.get("test_name") or "bandit"),
                    "location": f"{_norm(str(r.get('filename', '?')))}:{r.get('line_number', 0)}",
                    "description": str(r.get("issue_text", ""))[:400],
                    "tool": "bandit",
                    "rule_id": str(r.get("test_id", "")),
                    "confidence": str(r.get("issue_confidence", "")).lower() or None,
                }
            )
    # severity cao trước
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda f: order.get(str(f["severity"]), 9))
    errors = [str(e.get("filename", e)) for e in (d.get("errors") or [])]
    return {"tool": "bandit", **counts, "total": len(results), "findings": findings, "errors": errors}

"""detect-secrets `scan` JSON: {results: {"<file>": [{type, filename, hashed_secret, is_verified, line_number}]}}.

KHÔNG có secret thật trong output (chỉ hashed_secret) — parser cũng không chép hash ra finding.
"""

from __future__ import annotations

import json
from typing import Any


def parse_detect_secrets(text: str, *, max_findings: int = 20) -> dict[str, Any]:
    """→ {tool, count, files, findings[]}. is_verified=true → critical, còn lại high."""
    try:
        d = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        return {
            "tool": "detect-secrets",
            "count": 0,
            "files": 0,
            "findings": [],
            "errors": [f"parse_error: {e}"],
        }
    results = d.get("results") or {}
    findings: list[dict[str, Any]] = []
    count = 0
    for path, hits in results.items():
        for h in hits or []:
            count += 1
            if len(findings) < max_findings:
                findings.append(
                    {
                        "severity": "critical" if h.get("is_verified") else "high",
                        "cwe_or_category": "CWE-798",
                        "location": f"{h.get('filename') or path}:{h.get('line_number', 0)}",
                        "description": f"{h.get('type', 'secret')} — không in giá trị; thu hồi và chuyển sang biến môi trường/secret store",
                        "tool": "detect-secrets",
                        "rule_id": str(h.get("type", "")).replace(" ", "-").lower(),
                    }
                )
    return {"tool": "detect-secrets", "count": count, "files": len(results), "findings": findings}

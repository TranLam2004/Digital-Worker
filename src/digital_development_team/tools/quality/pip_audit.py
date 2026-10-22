"""pip-audit `-f json`: {dependencies[{name, version, vulns[{id, fix_versions, aliases, description}], skip_reason?}]}.

pip-audit không trả CVSS/severity → mọi vuln tính là `high` (bảo thủ, fail-closed) cho SEC-010 `high_cves == 0`.
"""

from __future__ import annotations

import json
from typing import Any


def parse_pip_audit(text: str, *, max_findings: int = 20) -> dict[str, Any]:
    """→ {tool, high_cves, vulnerable_packages, findings[], skipped[]}."""
    try:
        d = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        return {
            "tool": "pip-audit",
            "high_cves": 0,
            "vulnerable_packages": 0,
            "findings": [],
            "skipped": [],
            "errors": [f"parse_error: {e}"],
        }
    deps = d.get("dependencies") or []
    findings: list[dict[str, Any]] = []
    skipped: list[str] = []
    vulnerable = 0
    total = 0
    for dep in deps:
        if dep.get("skip_reason"):
            skipped.append(f"{dep.get('name')}: {dep['skip_reason']}")
            continue
        vulns = dep.get("vulns") or []
        if vulns:
            vulnerable += 1
        for v in vulns:
            total += 1
            if len(findings) < max_findings:
                fix = ", ".join(v.get("fix_versions") or []) or "chưa có bản vá"
                aliases = ", ".join(v.get("aliases") or [])
                findings.append(
                    {
                        "severity": "high",
                        "cwe_or_category": "dependency-vulnerability",
                        "location": f"{dep.get('name')}=={dep.get('version')}",
                        "description": f"{v.get('id')}{f' ({aliases})' if aliases else ''}: {str(v.get('description', ''))[:200]} — fix: {fix}",
                        "tool": "pip-audit",
                        "rule_id": str(v.get("id", "")),
                        "fix_versions": list(v.get("fix_versions") or []),
                    }
                )
    return {
        "tool": "pip-audit",
        "high_cves": total,
        "vulnerable_packages": vulnerable,
        "findings": findings,
        "skipped": skipped,
    }

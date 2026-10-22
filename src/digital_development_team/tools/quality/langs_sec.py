"""Parser kết quả quét phụ thuộc / SAST cho ngoài Python: npm audit · govulncheck · dotnet list package --vulnerable ·
SARIF (semgrep, osv-scanner, gosec…). Cùng dạng {tool, high, findings[]} để SEC-009/010 và machine gate đọc chung.

Không in secret; finding chỉ có vị trí, mã, mô tả ngắn.
"""

from __future__ import annotations

import json
from typing import Any

_HIGH = {"critical", "high", "error"}


def _loads(text: str) -> Any:
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return None


def _finding(
    severity: str, category: str, location: str, description: str, tool: str, rule: str
) -> dict[str, Any]:
    return {
        "severity": severity,
        "cwe_or_category": category[:80],
        "location": location[:200],
        "description": description[:200],
        "tool": tool,
        "rule_id": rule[:120],
    }


def parse_npm_audit(text: str) -> dict[str, Any]:
    d = _loads(text)
    if not isinstance(d, dict):
        return {"tool": "npm-audit", "high": 0, "findings": [], "errors": ["parse_error"]}
    if (
        "vulnerabilities" not in d and "metadata" not in d
    ):  # registry/mirror không hỗ trợ audit → KHÔNG phải "sạch"
        return {
            "tool": "npm-audit",
            "high": 0,
            "findings": [],
            "errors": [str(d.get("message") or "no audit data")[:200]],
        }
    meta = (d.get("metadata") or {}).get("vulnerabilities") or {}
    high = int(meta.get("high", 0)) + int(meta.get("critical", 0))
    findings = []
    for name, v in (d.get("vulnerabilities") or {}).items():
        via = [x for x in v.get("via") or [] if isinstance(x, dict)]
        title = via[0].get("title", "") if via else ""
        url = via[0].get("url", "") if via else ""
        findings.append(
            _finding(
                str(v.get("severity", "")),
                "dependency",
                f"{name} {v.get('range', '')}",
                title,
                "npm-audit",
                url,
            )
        )
    return {"tool": "npm-audit", "high": high, "findings": findings[:30]}


def parse_govulncheck(text: str) -> dict[str, Any]:
    """`govulncheck -json` là chuỗi object JSON nối nhau; tính các osv có `finding` (mã gọi tới hàm lỗi)."""
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    osv: dict[str, str] = {}
    hits: set[str] = set()
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        if not isinstance(obj, dict):
            continue
        if "osv" in obj and isinstance(obj["osv"], dict):
            osv[str(obj["osv"].get("id"))] = str(obj["osv"].get("summary") or "")
        f = obj.get("finding")
        if isinstance(f, dict) and any(
            isinstance(t, dict) and t.get("function") for t in f.get("trace") or []
        ):
            hits.add(str(f.get("osv")))
    findings = [
        _finding("high", "dependency", vid, osv.get(vid, ""), "govulncheck", vid) for vid in sorted(hits)
    ]
    return {"tool": "govulncheck", "high": len(hits), "findings": findings[:30]}


def parse_dotnet_vulnerable(text: str) -> dict[str, Any]:
    d = _loads(text)
    if not isinstance(d, dict):
        return {"tool": "dotnet-vulnerable", "high": 0, "findings": [], "errors": ["parse_error"]}
    findings = []
    high = 0
    for proj in d.get("projects") or []:
        for fw in proj.get("frameworks") or []:
            for kind in ("topLevelPackages", "transitivePackages"):
                for pkg in fw.get(kind) or []:
                    for v in pkg.get("vulnerabilities") or []:
                        sev = str(v.get("severity", "")).lower()
                        high += sev in _HIGH
                        findings.append(
                            _finding(
                                sev,
                                "dependency",
                                f"{pkg.get('id')} {pkg.get('resolvedVersion')}",
                                str(v.get("advisoryurl", "")),
                                "dotnet-vulnerable",
                                str(v.get("advisoryurl", "")),
                            )
                        )
    return {"tool": "dotnet-vulnerable", "high": high, "findings": findings[:30]}


def parse_sarif(text: str) -> dict[str, Any]:
    d = _loads(text)
    if not isinstance(d, dict):
        return {"tool": "sarif", "high": 0, "findings": [], "errors": ["parse_error"]}
    findings = []
    high = 0
    tool = "sarif"
    for run in d.get("runs") or []:
        tool = str(((run.get("tool") or {}).get("driver") or {}).get("name") or tool)
        for r in run.get("results") or []:
            level = str(r.get("level") or "warning").lower()
            sev = {"error": "high", "warning": "medium", "note": "low"}.get(level, "medium")
            high += sev == "high"
            loc = (r.get("locations") or [{}])[0].get("physicalLocation") or {}
            uri = (loc.get("artifactLocation") or {}).get("uri", "?")
            line = (loc.get("region") or {}).get("startLine", "?")
            findings.append(
                _finding(
                    sev,
                    "sast",
                    f"{uri}:{line}",
                    str((r.get("message") or {}).get("text", "")),
                    tool,
                    str(r.get("ruleId", "")),
                )
            )
    return {"tool": tool, "high": high, "findings": findings[:30]}

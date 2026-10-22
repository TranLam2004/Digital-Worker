"""Phase 6: quét phụ thuộc / SAST ngoài Python — npm audit · govulncheck · dotnet --vulnerable · SARIF; gộp kết quả."""

from __future__ import annotations

import json
from pathlib import Path

from digital_development_team.tools.quality.langs_sec import (
    parse_dotnet_vulnerable,
    parse_govulncheck,
    parse_npm_audit,
    parse_sarif,
)
from digital_development_team.tools.security import _merge

FIX = Path(__file__).parent / "fixtures" / "sec"

NPM = {
    "auditReportVersion": 2,
    "vulnerabilities": {
        "lodash": {
            "name": "lodash",
            "severity": "high",
            "via": [
                {"title": "Prototype Pollution", "url": "https://github.com/advisories/GHSA-p6mc-m468-83gw"}
            ],
            "range": "<4.17.21",
        }
    },
    "metadata": {"vulnerabilities": {"low": 0, "moderate": 0, "high": 1, "critical": 0, "total": 1}},
}
GOVULN = "\n".join(
    json.dumps(o)
    for o in (
        {"config": {"protocol_version": "v1.0.0"}},
        {"osv": {"id": "GO-2024-0001", "summary": "Panic in parser"}},
        {"osv": {"id": "GO-2024-0002", "summary": "Not reachable"}},
        {"finding": {"osv": "GO-2024-0001", "trace": [{"module": "example.com/x", "function": "Parse"}]}},
        {"finding": {"osv": "GO-2024-0002", "trace": [{"module": "example.com/y", "version": "v1.0.0"}]}},
    )
)
DOTNET = {
    "version": 1,
    "projects": [
        {
            "path": "App.csproj",
            "frameworks": [
                {
                    "framework": "net8.0",
                    "topLevelPackages": [
                        {
                            "id": "Newtonsoft.Json",
                            "resolvedVersion": "12.0.1",
                            "vulnerabilities": [
                                {
                                    "severity": "High",
                                    "advisoryurl": "https://github.com/advisories/GHSA-5crp-9r3c-p9vr",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}
SARIF = {
    "runs": [
        {
            "tool": {"driver": {"name": "semgrep"}},
            "results": [
                {
                    "ruleId": "javascript.express.security.audit.xss",
                    "level": "error",
                    "message": {"text": "User input flows into res.send"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/app.js"},
                                "region": {"startLine": 12},
                            }
                        }
                    ],
                },
                {"ruleId": "style", "level": "note", "message": {"text": "minor"}, "locations": []},
            ],
        }
    ]
}


def test_parsers_count_high_and_locate() -> None:
    npm = parse_npm_audit(json.dumps(NPM))
    assert npm["high"] == 1 and npm["findings"][0]["location"].startswith("lodash")
    go = parse_govulncheck(GOVULN)
    assert go["high"] == 1 and go["findings"][0]["rule_id"] == "GO-2024-0001"  # chỉ lỗ hổng mã gọi tới
    dn = parse_dotnet_vulnerable(json.dumps(DOTNET))
    assert dn["high"] == 1 and "Newtonsoft.Json 12.0.1" in dn["findings"][0]["location"]
    sf = parse_sarif(json.dumps(SARIF))
    assert sf["tool"] == "semgrep" and sf["high"] == 1 and sf["findings"][0]["location"] == "src/app.js:12"


def test_audit_unavailable_is_not_clean() -> None:
    """Mirror công ty không hỗ trợ audit (đo thật 2026-10-11): phải là lỗi/skipped, không phải 0 lỗ hổng."""
    r = parse_npm_audit((FIX / "npm_audit_error.json").read_text(encoding="utf-8"))
    assert r["errors"] and r["high"] == 0


def test_merge_scanners() -> None:
    skipped = {"status": "skipped", "summary": "govulncheck không có trong PATH", "high": 0}
    assert (
        _merge([skipped, {**skipped, "summary": "pip-audit: không có requirements"}], "deps")["status"]
        == "skipped"
    )
    passed = {"status": "pass", "summary": "pip-audit: 0", "high_cves": 0, "findings": []}
    bad = {
        "status": "fail",
        "summary": "npm-audit: 1 high",
        "high": 1,
        "findings": [{"x": 1}],
        "output_ref": "artifact://a",
    }
    m = _merge([passed, skipped, bad], "deps")
    assert m["status"] == "fail" and m["high"] == m["high_cves"] == 1 and m["output_ref"] == "artifact://a"
    assert _merge([passed, skipped], "deps")["status"] == "pass"

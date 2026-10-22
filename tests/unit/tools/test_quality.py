"""tools.quality — parser thuần trên output mẫu của bandit · pip-audit · detect-secrets · diff-cover · coverage.xml."""

from __future__ import annotations

import json

from digital_development_team.tools.quality import (
    parse_bandit,
    parse_coverage_xml,
    parse_detect_secrets,
    parse_diff_cover,
    parse_pip_audit,
)

BANDIT = json.dumps(
    {
        "errors": [],
        "results": [
            {
                "filename": "shop/x.py",
                "line_number": 12,
                "issue_severity": "LOW",
                "issue_confidence": "HIGH",
                "issue_text": "assert used",
                "test_id": "B101",
                "test_name": "assert_used",
                "issue_cwe": {"id": 703},
            },
            {
                "filename": "shop/y.py",
                "line_number": 3,
                "issue_severity": "HIGH",
                "issue_confidence": "HIGH",
                "issue_text": "subprocess with shell=True",
                "test_id": "B602",
                "test_name": "subprocess_popen_with_shell_equals_true",
                "issue_cwe": {"id": 78},
            },
            {
                "filename": "shop/z.py",
                "line_number": 9,
                "issue_severity": "MEDIUM",
                "issue_confidence": "LOW",
                "issue_text": "hardcoded tmp",
                "test_id": "B108",
                "test_name": "hardcoded_tmp_directory",
            },
        ],
    }
)
PIP_AUDIT = json.dumps(
    {
        "dependencies": [
            {
                "name": "requests",
                "version": "2.19.0",
                "vulns": [
                    {
                        "id": "PYSEC-2018-28",
                        "fix_versions": ["2.20.0"],
                        "aliases": ["CVE-2018-18074"],
                        "description": "credentials leak",
                    }
                ],
            },
            {"name": "pydantic", "version": "2.7.0", "vulns": []},
            {"name": "local-pkg", "version": "0.1", "skip_reason": "not on PyPI"},
        ],
        "fixes": [],
    }
)
DETECT_SECRETS = json.dumps(
    {
        "version": "1.5.0",
        "results": {
            "config/settings.py": [
                {
                    "type": "Secret Keyword",
                    "filename": "config/settings.py",
                    "hashed_secret": "abc",
                    "is_verified": False,
                    "line_number": 7,
                },
                {
                    "type": "AWS Access Key",
                    "filename": "config/settings.py",
                    "hashed_secret": "def",
                    "is_verified": True,
                    "line_number": 9,
                },
            ]
        },
    }
)
DIFF_COVER = json.dumps(
    {
        "report_name": "XML",
        "diff_name": "main...HEAD, staged and unstaged changes",
        "src_stats": {
            "shop/inventory.py": {
                "percent_covered": 75.0,
                "violation_lines": [31, 32],
                "violations": [[31, None], [32, None]],
            },
            "tests/test_inventory.py": {"percent_covered": 100.0, "violation_lines": [], "violations": []},
        },
        "total_num_lines": 20,
        "total_num_violations": 2,
        "total_percent_covered": 90,
    }
)
COVERAGE_XML = """<?xml version="1.0" ?>
<coverage version="7.6" timestamp="1" lines-valid="100" lines-covered="85" line-rate="0.85" branch-rate="0">
  <packages><package name="shop" line-rate="0.85">
    <classes>
      <class name="inventory.py" filename="shop/inventory.py" line-rate="0.8"><lines/></class>
      <class name="cart.py" filename="shop/cart.py" line-rate="0.9"><lines/></class>
    </classes>
  </package></packages>
</coverage>"""


def test_bandit() -> None:
    r = parse_bandit(BANDIT)
    assert (r["high"], r["medium"], r["low"], r["total"]) == (1, 1, 1, 3)
    assert r["findings"][0]["severity"] == "high" and r["findings"][0]["location"] == "shop/y.py:3"
    assert r["findings"][0]["cwe_or_category"] == "CWE-78" and r["findings"][0]["rule_id"] == "B602"
    assert (
        r["findings"][1]["cwe_or_category"] == "hardcoded_tmp_directory"
    )  # không có CWE → tên test (medium xếp giữa)
    assert parse_bandit("")["total"] == 0 and parse_bandit("garbage")["errors"]


def test_pip_audit() -> None:
    r = parse_pip_audit(PIP_AUDIT)
    assert (
        r["high_cves"] == 1 and r["vulnerable_packages"] == 1 and r["skipped"] == ["local-pkg: not on PyPI"]
    )
    f = r["findings"][0]
    assert (
        f["location"] == "requests==2.19.0"
        and "CVE-2018-18074" in f["description"]
        and f["fix_versions"] == ["2.20.0"]
    )


def test_detect_secrets_never_leaks_value() -> None:
    r = parse_detect_secrets(DETECT_SECRETS)
    assert r["count"] == 2 and r["files"] == 1
    assert r["findings"][1]["severity"] == "critical" and r["findings"][0]["severity"] == "high"
    assert "abc" not in json.dumps(r) and "def" not in json.dumps(r["findings"])  # không chép hashed_secret
    assert r["findings"][0]["location"] == "config/settings.py:7"


def test_diff_cover() -> None:
    r = parse_diff_cover(DIFF_COVER)
    assert r["diff_pct"] == 90.0 and r["new_lines"] == 20 and r["uncovered_count"] == 2
    assert r["uncovered_new_lines"] == [{"path": "shop/inventory.py", "lines": [31, 32]}]
    assert (
        parse_diff_cover(json.dumps({"src_stats": {}, "total_num_lines": 0, "total_percent_covered": 100}))[
            "diff_pct"
        ]
        is None
    )


def test_coverage_xml() -> None:
    r = parse_coverage_xml(COVERAGE_XML)
    assert r["total_pct"] == 85.0 and r["lines_valid"] == 100 and r["lines_covered"] == 85
    assert r["files"] == [{"path": "shop/inventory.py", "pct": 80.0}, {"path": "shop/cart.py", "pct": 90.0}]
    assert parse_coverage_xml("<broken")["total_pct"] is None

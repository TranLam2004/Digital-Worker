"""security.scan — bandit/detect-secrets thật trên worktree (skip nếu thiếu binary); policy cho security (readonly) gọi."""

from __future__ import annotations

import json
import shutil

import pytest

from digital_development_team.tools.security import BINARIES, tool_available
from tests.unit.tools.test_tools import _dispatch

VULN = """import subprocess

def run(cmd):
    return subprocess.call(cmd, shell=True)  # B602 HIGH

def calc(expr):
    return eval(expr)  # B307 MEDIUM
"""
FAKE_SECRET_FILE = (
    'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\nTOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"\n'  # noqa: S105 - giá trị ví dụ công khai (AWS docs), chỉ để scanner bắt
)


@pytest.mark.skipif(
    not (tool_available("sast") and tool_available("secrets")), reason="thiếu bandit/detect-secrets"
)
def test_security_scan_real_scanners(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    (wt / "vuln.py").write_text(VULN, encoding="utf-8")
    (wt / "settings.py").write_text(FAKE_SECRET_FILE, encoding="utf-8")
    r = _dispatch(rt, tooling, agents, wt, "security", "security.scan", paths=["vuln.py", "settings.py"])
    assert r.status == "ok", r.summary
    s = r.structured
    assert s["targets"] == ["sast", "secrets"] and s["files_scanned"] == 2
    assert s["sast"]["status"] == "fail" and s["sast"]["high"] >= 1
    assert any(f["rule_id"] == "B602" and f["location"].startswith("vuln.py:") for f in s["sast"]["findings"])
    assert s["secrets"]["status"] == "fail" and s["secrets"]["count"] >= 1
    assert all(f["location"].startswith("settings.py:") for f in s["secrets"]["findings"])
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(s)  # không in secret
    assert len(r.artifact_refs) == 2 and tooling.artifacts.read(r.artifact_refs[0]).startswith("$ bandit")
    # không ghi gì vào worktree ngoài 2 file test
    assert sorted(p.name for p in wt.iterdir() if p.name not in {".git"}) == sorted(
        ["REPO.md", "calc.py", "pytest.ini", "settings.py", "tests", "vuln.py", ".gitignore"]
    )


def test_security_scan_defaults_and_deps_skip(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    r = _dispatch(rt, tooling, agents, wt, "security", "security.scan", targets=["deps"])
    assert r.status == "ok" and r.structured["deps"]["status"] == "skipped", r.summary
    r = _dispatch(rt, tooling, agents, wt, "security", "security.scan", targets=["nope"])
    assert r.status == "error" and r.error is not None and r.error.code == "BAD_ARGS"
    # dev không có grant security.scan → policy deny lớp 1
    r = _dispatch(rt, tooling, agents, wt, "dev", "security.scan")
    assert r.status == "denied"


def test_binaries_table() -> None:
    assert set(BINARIES) == {"sast", "secrets", "deps"}
    assert tool_available("sast") == (shutil.which("bandit") is not None)

"""test.coverage (pytest-cov + diff-cover) và test.run(affected) — chạy thật trên worktree mini_calc; worktree không đổi."""

from __future__ import annotations

import pytest

from digital_development_team.tools.coverage import coverage_available, read_baseline
from tests.unit.tools.test_tools import _dispatch


def _snapshot(wt):  # type: ignore[no-untyped-def]
    return sorted(
        str(p.relative_to(wt))
        for p in wt.rglob("*")
        if ".git" not in p.parts and "__pycache__" not in p.parts
    )


@pytest.mark.skipif(coverage_available() is not None, reason=str(coverage_available()))
def test_coverage_tool_measures_changed_lines(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    # Dev sửa calc.py (chưa có test cho dòng mới) → uncovered_new_lines phải bắt được
    calc = wt / "calc.py"
    calc.write_text(
        calc.read_text(encoding="utf-8").replace(
            "def mean(xs: list[float]) -> float:\n    return sum(xs) / len(xs)\n",
            "def mean(xs: list[float]) -> float:\n    if not xs:\n        raise ValueError('mean of empty list')\n    return sum(xs) / len(xs)\n",
        ),
        encoding="utf-8",
    )
    before = _snapshot(wt)
    r = _dispatch(rt, tooling, agents, wt, "verifier", "test.coverage")
    assert r.status == "ok", r.summary
    s = r.structured
    assert s["status"] == "pass" and s["total_pct"] is not None and 0 < s["total_pct"] <= 100
    assert s["test"]["status"] == "pass"
    assert s["new_lines"] >= 2 and any(u["path"].endswith("calc.py") for u in s["uncovered_new_lines"]), s
    assert s["uncovered_count"] >= 1 and s["baseline_pct"] is None and s["delta"] is None  # chưa có baseline
    assert r.artifact_refs and tooling.artifacts.read(r.artifact_refs[0]).startswith("$ COVERAGE_FILE=")
    assert _snapshot(wt) == before  # không ghi .coverage / coverage.xml vào worktree
    assert (
        read_baseline(rt.layout.memory_dir / "coverage_baseline.json") is None
    )  # tool không tự tạo baseline
    # dev không có grant
    assert _dispatch(rt, tooling, agents, wt, "dev", "test.coverage").status == "denied"


def test_run_affected_mode(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    r = _dispatch(rt, tooling, agents, wt, "dev", "test.run", cmd="pytest -q", affected=True)
    assert r.status == "ok", r.summary
    assert r.structured["affected_mode"] in {"testmon", "fallback_full"} and r.structured["status"] == "pass"
    assert "TESTMON_DATAFILE" in r.structured["cmd"] or r.structured["affected_mode"] == "fallback_full"
    assert not (wt / ".testmondata").exists()
    r2 = _dispatch(rt, tooling, agents, wt, "verifier", "test.run", affected=True)
    assert r2.status == "denied" and r2.structured["rule"] == "verifier_filter"

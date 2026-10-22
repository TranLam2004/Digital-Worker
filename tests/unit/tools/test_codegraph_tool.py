"""codegraph.* qua CodeGraph CLI thật (skip nếu thiếu binary): init index repo chính, search/callers/impact/node, stale."""

from __future__ import annotations

import pytest

from digital_development_team.core.context import CancelToken
from digital_development_team.tools.codegraph import (
    INDEX_DIR,
    cg_available,
    ensure_index,
    is_indexed,
    status_of,
)
from tests.unit.tools.test_tools import _dispatch

pytestmark = pytest.mark.skipif(not cg_available(), reason="không có codegraph CLI")


def test_codegraph_tools_end_to_end(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    repo = rt.repo
    assert repo is not None
    # chưa index → fallback PyGraph (ast) trả lời ngay, không lỗi
    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.search", query="mean")
    assert r.status == "ok" and r.structured.get("backend") == "pyast"
    init = ensure_index(tooling.sandbox, rt.config.policy.sandbox, repo, CancelToken())
    assert init["status"] == "ok", init
    assert is_indexed(repo) and f"{INDEX_DIR}/" in (repo / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    )
    st = status_of(tooling.sandbox, rt.config.policy.sandbox, repo, CancelToken())
    assert st["indexed"] and st["nodes"] and st["languages"] == ["python"]

    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.search", query="mean")
    assert r.status == "ok" and "backend" not in r.structured, r.summary  # giờ đi CLI thật
    assert any(s["name"] == "mean" and s["path"] == "calc.py" for s in r.structured["symbols"])
    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.callers", symbol="mean")
    assert (
        r.status == "ok" and r.structured["test_count"] >= 1 and "tests/test_calc.py" in r.structured["files"]
    )
    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.impact", symbol="mean")
    assert (
        r.status == "ok"
        and "tests/test_calc.py" in r.structured["affected_tests"]
        and r.structured["risk"] in {"low", "medium", "high"}
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "codegraph.node", symbol="mean")
    assert (
        r.status == "ok"
        and r.structured["path"] == "calc.py"
        and "def mean" in (r.structured["source"] or "")
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "codegraph.node", symbol="no_such_symbol_xyz")
    assert r.status == "error" and r.error is not None and r.error.code == "SYMBOL_NOT_FOUND"
    # worktree sửa calc.py → stale flag
    (wt / "calc.py").write_text(
        (wt / "calc.py").read_text(encoding="utf-8") + "\n\ndef extra():\n    return 1\n", encoding="utf-8"
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "codegraph.callers", symbol="mean")
    assert r.status == "ok" and r.structured.get("stale") is True and "calc.py" in r.structured["stale_files"]
    # verifier không có grant codegraph (độc lập)
    assert _dispatch(rt, tooling, agents, wt, "verifier", "codegraph.search", query="mean").status == "denied"

"""PyGraph (stdlib ast) — backend codegraph.* không cần cài thêm: search/callers/callees/impact/node + fallback trong tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from digital_development_team.tools import codegraph as cgmod
from digital_development_team.tools.pygraph import PyGraph, pygraph_for
from tests.conftest import TESTDATA
from tests.unit.tools.test_tools import _dispatch

SHOP = TESTDATA / "repos" / "shop_api"


def test_pygraph_index_and_queries() -> None:
    g = PyGraph(SHOP)
    st = g.stats
    assert st["files"] >= 7 and st["symbols"] >= 15 and st["calls"] >= 10
    hit = g.search("reserve", limit=5)
    assert (
        hit
        and hit[0]["name"] == "reserve"
        and hit[0]["kind"] == "method"
        and hit[0]["path"] == "shop/inventory.py"
    )
    assert hit[0]["qualified_name"] == "Inventory::reserve" and hit[0]["signature"] == "(self, sku, qty)"
    callers = g.callers("reserve")
    assert {c["path"] for c in callers} == {"tests/test_inventory.py"} and len(callers) >= 2
    callees = g.callees("with_tax")
    assert (
        any(c["name"] == "Decimal" for c in callees) or callees == []
    )  # Decimal là import, không phải symbol repo
    imp = g.impact("reserve", depth=2)
    assert "tests/test_inventory.py" in imp["tests"] and imp["depth_reached"] >= 1 and not imp["truncated"]
    assert (
        g.exact("Cart") and g.exact("Cart")[0].kind == "class" and g.source(g.exact("Cart")[0], max_lines=3)
    )
    assert g.exact("nope") == [] and g.search("zzz") == []


def test_pygraph_refresh_on_change(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return g()\n\ndef g():\n    return 1\n", encoding="utf-8")
    g = pygraph_for(tmp_path)
    assert [c["name"] for c in g.callers("g")] == ["f"]
    (tmp_path / "a.py").write_text("def f():\n    return 1\n\ndef g():\n    return 1\n", encoding="utf-8")
    g2 = pygraph_for(tmp_path)
    assert g2 is g and g2.callers("g") == []  # cùng object, đã refresh theo mtime/size


def test_codegraph_tool_falls_back_to_pyast(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    monkeypatch.setattr(cgmod, "cg_available", lambda: False)  # không có CLI → vẫn trả lời
    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.search", query="mean")
    assert r.status == "ok" and r.structured["backend"] == "pyast", r.summary
    assert any(s["name"] == "mean" and s["path"] == "calc.py" for s in r.structured["symbols"])
    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.callers", symbol="mean")
    assert (
        r.status == "ok" and r.structured["test_count"] >= 1 and "tests/test_calc.py" in r.structured["files"]
    )
    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.impact", symbol="mean")
    assert (
        r.status == "ok"
        and "tests/test_calc.py" in r.structured["affected_tests"]
        and r.structured["backend"] == "pyast"
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "codegraph.node", symbol="mean")
    assert r.status == "ok" and "def mean" in (r.structured["source"] or "")
    r = _dispatch(rt, tooling, agents, wt, "dev", "codegraph.node", symbol="nope_xyz")
    assert r.status == "error" and r.error is not None and r.error.code == "SYMBOL_NOT_FOUND"
    # worktree chỉnh sửa → PyGraph đọc worktree hiện tại nên thấy ngay symbol mới
    (wt / "calc.py").write_text(
        (wt / "calc.py").read_text(encoding="utf-8") + "\n\ndef extra():\n    return mean([1.0])\n",
        encoding="utf-8",
    )
    r = _dispatch(rt, tooling, agents, wt, "planner", "codegraph.callers", symbol="mean")
    assert any(c["name"] == "extra" for c in r.structured["callers"])

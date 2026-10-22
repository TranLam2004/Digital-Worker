"""FMEA thuần + diff → symbol đổi (ast, method-not-class) + gộp blast radius với impact giả."""

from __future__ import annotations

from pathlib import Path

from digital_development_team.risk.blast import fmea, impact_risk
from digital_development_team.runner.stages.blast_radius import (
    changed_symbols_py,
    compute_blast_radius,
    hunk_new_ranges,
)

DIFF = """diff --git a/shop/inventory.py b/shop/inventory.py
--- a/shop/inventory.py
+++ b/shop/inventory.py
@@ -27,6 +27,9 @@ class Inventory:
     def reserve(self, sku: str, qty: int) -> None:
         if qty <= 0:
             raise ValueError("qty must be positive")
+        avail = self.available(sku)
+        if qty > avail:
+            raise InsufficientStock(sku, qty, avail)
         self._reserved[sku] = self._reserved.get(sku, 0) + qty
diff --git a/tests/test_inventory.py b/tests/test_inventory.py
--- a/tests/test_inventory.py
+++ b/tests/test_inventory.py
@@ -1,3 +1,3 @@
-from shop import Inventory
+from shop import InsufficientStock, Inventory
"""

SRC = """class InsufficientStock(Exception):
    pass


class Inventory:
    TABLE = "inv"

    def __init__(self) -> None:
        self._stock = {}

    def available(self, sku):
        return self._stock.get(sku, 0)

    def reserve(self, sku, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        avail = self.available(sku)
        if qty > avail:
            raise InsufficientStock(sku, qty, avail)
        self._stock[sku] = avail - qty


def helper():
    return 1
"""


def test_fmea_thresholds() -> None:
    low = fmea(affected_files=1, depth_reached=1, changed_symbols=1, coverage_changed_pct=100.0)
    assert (low.severity, low.occurrence, low.detection, low.risk) == (2, 3, 1, "low") and low.rpn == 6
    unknown = fmea(
        affected_files=12,
        depth_reached=2,
        changed_symbols=6,
        coverage_changed_pct=None,
        touches_sensitive=True,
    )
    assert unknown.detection == 7 and unknown.severity == 6 and unknown.occurrence == 5 and unknown.rpn == 210
    assert unknown.risk == "high"
    mid = fmea(affected_files=5, depth_reached=2, changed_symbols=2, coverage_changed_pct=50.0)
    assert (mid.severity, mid.occurrence, mid.detection) == (2, 4, 6) and mid.rpn == 48 and mid.risk == "low"
    worse = fmea(affected_files=10, depth_reached=2, changed_symbols=2, coverage_changed_pct=20.0)
    assert (worse.severity, worse.detection) == (3, 8) and worse.rpn == 96 and worse.risk == "medium"
    assert (
        fmea(affected_files=0, depth_reached=0, changed_symbols=0, coverage_changed_pct=0.0).detection == 10
    )


def test_impact_risk_contract() -> None:
    assert (
        impact_risk(2) == "low"
        and impact_risk(3) == "medium"
        and impact_risk(8) == "medium"
        and impact_risk(9) == "high"
    )
    assert impact_risk(1, touches_public_api=True) == "high"


def test_hunk_ranges_and_changed_symbols() -> None:
    ranges = hunk_new_ranges(DIFF)
    assert ranges["shop/inventory.py"] == [(27, 35)] and ranges["tests/test_inventory.py"] == [(1, 3)]
    syms = changed_symbols_py("shop/inventory.py", SRC, [(16, 18)])  # dòng trong reserve
    assert [(s.kind, s.qualified) for s in syms] == [("method", "Inventory::reserve")]
    # đổi dòng class-level (TABLE = …) → class tính là đổi, method không
    syms2 = changed_symbols_py("shop/inventory.py", SRC, [(6, 6)])
    assert [(s.kind, s.symbol) for s in syms2] == [("class", "Inventory")]
    # hàm top-level
    assert [s.symbol for s in changed_symbols_py("shop/inventory.py", SRC, [(23, 23)])] == ["helper"]
    assert changed_symbols_py("x.py", "def (", [(1, 1)]) == []  # syntax error → rỗng, không nổ


def test_compute_blast_radius_aggregates(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "inventory.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_inventory.py").write_text("from shop import Inventory\n", encoding="utf-8")
    diff = DIFF.replace("@@ -27,6 +27,9 @@", "@@ -16,3 +16,3 @@")  # trỏ vào reserve trong SRC

    def impact(sym: str):  # type: ignore[no-untyped-def]
        assert sym == "reserve"
        return {
            "affected_files": ["shop/orders.py", "shop/inventory.py", "tests/test_inventory.py"],
            "affected_tests": ["tests/test_inventory.py"],
            "risk": "low",
        }

    r = compute_blast_radius(
        tmp_path,
        diff,
        ["shop/inventory.py", "tests/test_inventory.py"],
        impact,
        depth=2,
        coverage_diff_pct=66.7,
        touches_sensitive=False,
        rpn_high=200,
        rpn_medium=80,
    )
    assert (
        r["status"] == "pass"
        and r["changed_count"] == 1
        and r["changed"][0]["qualified"] == "Inventory::reserve"
    )
    assert r["affected_files"] == ["shop/orders.py"] and r["affected_tests"] == ["tests/test_inventory.py"]
    assert r["depth_reached"] == 2 and r["fmea"]["detection"] == 4 and r["risk"] in {"low", "medium"}
    skipped = compute_blast_radius(
        tmp_path,
        diff,
        ["shop/inventory.py"],
        None,
        depth=2,
        coverage_diff_pct=None,
        touches_sensitive=False,
        rpn_high=200,
        rpn_medium=80,
    )
    assert skipped["status"] == "skipped" and skipped["changed_count"] == 1

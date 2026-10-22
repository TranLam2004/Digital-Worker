from __future__ import annotations

from digital_development_team.checklist import ChecklistRegistry, run_regex
from digital_development_team.checklist.loader import normalize_when

DIFF_BAD = """--- a/app.py
+++ b/app.py
@@ -1,3 +1,8 @@
+print("debug")
+try:
+    x()
+except Exception: pass
+api_key = "sk-live-abcdefghijklmnop"
+# TODO fix later
"""
DIFF_OK = (
    "--- a/calc.py\n+++ b/calc.py\n@@\n+    if not xs:\n+        raise ValueError('mean of empty list')\n"
)


def test_registry_loads_base_and_overlays() -> None:
    reg = ChecklistRegistry()
    assert {
        "_base/code",
        "_base/security",
        "_base/spec",
        "_base/test",
        "project-type/web-api",
        "domain/banking",
    } <= set(reg.keys())
    assert reg.lite_security_ids() == ["SEC-001", "SEC-002", "SEC-003", "SEC-004", "SEC-005", "SEC-006"]
    ids = [i.id for i in reg.items()]
    assert len(ids) == len(set(ids)) and "CODE-001" in ids
    # biểu thức ngoài dialect (generator any(... for ...)) được ghi nhận, không làm vỡ registry
    assert (
        all("for " in reg.items_by_id(i).applies_when for i in reg.unsupported)
        if hasattr(reg, "items_by_id")
        else reg.unsupported
    )
    assert normalize_when("not spec.goal.contains('skip')") == "not contains(spec.goal, 'skip')"


def test_select_and_regex_run() -> None:
    reg = ChecklistRegistry()
    scope = {
        "intake": {"type": "bug"},
        "spec": {"goal": "fix mean", "acceptance_criteria": []},
        "plan": {"candidates": [{"callers_affected": []}]},
        "files_touched": ["calc.py"],
        "state": {"tier": "T1"},
    }
    items = reg.select("patch", scope)
    ids = {i.id for i in items}
    assert "CODE-001" in ids and "SEC-001" in ids and len(items) <= 30
    assert items[0].severity == "blocking"  # sort theo severity
    run = run_regex(items, DIFF_BAD)
    fails = {r.id for r in run.fails}
    assert "CODE-001" in fails and "CODE-005" in fails and "SEC-001" in fails and "CODE-002" in fails
    assert run.blocking_fail and run.verdict == "fail" and not run.clean
    assert not run.security_regex_pass(reg.lite_security_ids())
    f = run.facts(reg.lite_security_ids())
    assert f["blocking_fail"] is True and "SEC-001" in f["failed_ids"]
    ok = run_regex(items, DIFF_OK)
    assert (
        ok.fails == []
        and ok.clean
        and ok.verdict == "pass"
        and ok.security_regex_pass(reg.lite_security_ids())
    )
    assert ok.skipped  # diff_inspection/llm/test không chạy ở pha máy
    # project-type/domain overlay
    web = reg.select("patch", {**scope, "files_touched": ["src/api/users.py"]}, project_type="web-api")
    assert any(i.id.startswith("WEB-") for i in web)


def test_run_test_refs_thresholds() -> None:
    from digital_development_team.checklist.runner import run_test_refs

    reg = ChecklistRegistry()
    scope = {
        "intake": {"type": "bug"},
        "spec": {"goal": "x", "acceptance_criteria": []},
        "plan": {"candidates": []},
        "files_touched": ["calc.py", "requirements.txt"],
        "state": {"tier": "T1"},
        "repo": {"tooling": {"coverage": True, "sast": True}},
    }
    items = reg.select("patch", scope)
    ids = {i.id for i in items}
    assert {"TEST-004", "TEST-005", "SEC-009", "SEC-010"} <= ids, ids
    res = run_test_refs(
        items,
        {
            "coverage_delta": -3.5,
            "uncovered_new_lines": 2,
            "sast_scan": {"high_findings": 0},
            "dependency_audit": {"high_cves": 1},
        },
    )
    by = {r.id: r for r in res}
    assert by["TEST-004"].result == "fail" and by["TEST-005"].result == "pass"
    assert by["SEC-009"].result == "pass" and by["SEC-010"].result == "fail"
    assert "coverage_delta = -3.5" in by["TEST-004"].to_evidence()["evidence"]
    assert run_test_refs(items, {}) == []  # không có số đo → không đoán

"""Artifact mẫu HỢP LỆ theo schema cho --stub-stages (0 LLM). Dùng để chạy state machine, demo, test runner.

Mỗi builder nhận `o` = override dict (từ stub_handlers(overrides)) để test rẽ nhánh: T3 → G0, verdict fail → rework…
"""

from __future__ import annotations

from typing import Any

from digital_development_team.core.context import Clock
from digital_development_team.core.enums import Tier


def stub_item(issue: str, clock: Clock, o: dict[str, Any] | None = None) -> dict[str, Any]:
    o = o or {}
    return {
        "id": issue,
        "source": "stub",
        "source_id": issue.lower(),
        "title": o.get("title", "mean([]) raises ZeroDivisionError"),
        "body": o.get(
            "body", "Calling mean on an empty list crashes with ZeroDivisionError. Expected: ValueError."
        ),
        "labels": ["bug"],
        "attachments": [],
        "priority_hint": None,
        "blocked_by": [],
        "received_at": clock.now().isoformat(),
        "raw": {},
    }


def stub_intake(issue: str, o: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_id": issue,
        "type": o.get("type", "bug"),
        "severity": o.get("severity", "P2"),
        "complexity": o.get("complexity", "small"),
        "risk_tier": o.get("risk_tier", "T1"),
        "route": o.get("route", "standard"),
        "confidence": o.get("confidence", 0.84),
        "affected_area": "calc",
        "entry_hints": ["mean", "ZeroDivisionError"],
        "requires_human_before_code": o.get("requires_human_before_code", False),
        "rationale": "Stack trace rõ, một hàm, có test sẵn.",
        **({"question": o["question"]} if o.get("question") else {}),
    }


def stub_plan(issue: str, o: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_id": issue,
        "localize_method": ["ripgrep", "skeleton"],
        "candidates": [
            {
                "rank": 1,
                "files": [{"path": "calc.py", "symbols": ["mean"], "lines": "10-14"}],
                "confidence": 0.9,
                "reason": "Hàm mean chia cho len(xs) không kiểm rỗng.",
                "callers_affected": [],
            }
        ],
        "steps": [
            {"order": 1, "action": "write_failing_test", "target": "tests/test_calc.py"},
            {"order": 2, "action": "modify", "target": "calc.py"},
            {"order": 3, "action": "run_test", "target": "pytest -q"},
        ],
        "hot_files_touched": [],
        "rollback": "git revert HEAD",
        "estimated_complexity": "small",
        "needs_containment": False,
    }


def stub_spec(issue: str, o: dict[str, Any], *, tier: Tier = Tier.T1) -> dict[str, Any]:
    return {
        "issue_id": issue,
        "goal": "mean([]) raise ValueError thay vì ZeroDivisionError",
        "acceptance_criteria": [
            {
                "id": "AC1",
                "text": "mean([]) raise ValueError",
                "verify_by": "test",
                "test_ref": "tests/test_calc.py::test_mean_empty",
            },
            {
                "id": "AC2",
                "text": "mean([1,2,3]) == 2",
                "verify_by": "test",
                "test_ref": "tests/test_calc.py::test_mean",
            },
        ],
        "allowed_files": ["calc.py", "tests/test_calc.py"],
        "forbidden_files": [],
        "test_command": "pytest -q tests/test_calc.py",
        "regression_command": "pytest -q",
        "rollback_plan": "git revert HEAD",
        "out_of_scope": [],
        "ambiguities": list(o.get("ambiguities", [])),
        "estimated_tier": o.get("estimated_tier", tier.value),
    }


def stub_patch(issue: str, o: dict[str, Any], *, attempt: int) -> dict[str, Any]:
    return {
        "diff": (
            "--- a/calc.py\n+++ b/calc.py\n@@ -10,3 +10,5 @@\n def mean(xs):\n"
            f"+    if not xs:  # attempt {attempt}\n+        raise ValueError('mean of empty list')\n     return sum(xs) / len(xs)\n"
        ),
        "files_touched": ["calc.py", "tests/test_calc.py"],
        "impl_log": f"Thêm guard rỗng (lần {attempt}). Test mới test_mean_empty.",
        "base_commit": "0000000",
        "head_commit": f"stub{attempt:03d}",
        "handoff": None,
    }


def stub_evidence(issue: str, o: dict[str, Any], *, version: int) -> dict[str, Any]:
    verdict = o.get("verdict", "pass")
    ev: dict[str, Any] = {
        "issue_id": issue,
        "version": version,
        "verdict": verdict,
        "machine_gate": {
            "build": {"status": "pass"},
            "lint": {"status": "pass"},
            "typecheck": {"status": "skipped"},
            "unit_test": {"status": "pass", "summary": "4 passed"},
        },
        "criteria": [
            {
                "id": "AC1",
                "result": "pass" if verdict == "pass" else "fail",
                "evidence": "tests/test_calc.py::test_mean_empty PASSED",
            },
            {"id": "AC2", "result": "pass", "evidence": "tests/test_calc.py::test_mean PASSED"},
        ],
        "regression": {"ran": True, "result": "pass", "evidence": "pytest -q: 12 passed"},
        "adversarial_attempted": ["None input", "list of one element"],
        "adversarial_findings": [],
        "unverifiable": [],
        "scope_violation": [],
    }
    if verdict == "fail":
        ev["failure_feedback_for_dev"] = "calc.py:11 guard rỗng thiếu — mean([]) vẫn ZeroDivisionError."
    return ev


def stub_sec_report(issue: str, o: dict[str, Any]) -> dict[str, Any]:
    checks = (
        "no_secret_in_code",
        "no_secret_in_log",
        "no_privilege_widening",
        "no_new_dependency",
        "no_disabled_check",
        "no_prompt_injection_trace",
    )
    return {
        "issue_id": issue,
        "mode": o.get("mode", "lite"),
        "verdict": o.get("verdict", "pass"),
        "checklist": {c: {"result": "pass"} for c in checks},
        "findings": list(o.get("findings", [])),
        **({"incident_5c": o["incident_5c"]} if o.get("incident_5c") else {}),
    }


def stub_decision(issue: str, o: dict[str, Any], *, tier: Tier, rework: int) -> dict[str, Any]:
    confidence = float(o.get("confidence", 0.9))
    requires_human = bool(o.get("requires_human", tier in (Tier.T2, Tier.T3)))
    return {
        "issue_id": issue,
        "action": o.get("action", "merge"),
        "final_tier": tier.value,
        "tier_bumped_because": [],
        "confidence": confidence,
        "confidence_breakdown": {
            "test_signal": 0.30,
            "verifier_agreement": 0.25,
            "spec_clarity": 0.20,
            "familiarity": 0.15,
            "rework_penalty": min(0.1 * rework, 0.2),
        },
        "requires_human": requires_human,
        "rationale": "Mọi AC pass, regression pass, security lite pass. Patch nhỏ, một file.",
        "summary_for_human": {
            "title": "mean([]) raises ZeroDivisionError",
            "ac_pass_count": "2/2",
            "files_changed": 2,
            "lines_added": 2,
            "lines_removed": 0,
            "regression": "pass",
            "security": "pass",
            "rework_count": rework,
            "tokens_used": 0,
            "wall_time_sec": 0,
            "top_risk": "Không có",
        },
    }


def stub_answer(issue: str, o: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "issue_id": issue,
        "answer": o.get(
            "answer",
            "mean() trả float vì chia bằng toán tử / (chia thực trong Python 3), kể cả khi đầu vào toàn số nguyên.",
        ),
        "references": o.get("references", ["calc.py#mean"]),
        "confidence": o.get("confidence", 0.8),
    }
    if o.get("follow_up"):
        out["follow_up"] = o["follow_up"]
    return out

"""Contest contracts checked by code, independently of agent verdicts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def mvs_mapping_problems(match: Mapping[str, Any], spec: Mapping[str, Any]) -> list[str]:
    if not match:
        return []  # Daily issues can reuse a spec fixture without an organizer MVS contract.
    required = {str(m["id"]) for m in match.get("mvs", [])}
    covered: set[str] = set()
    problems = []
    folder = str(match.get("work_folder") or ".").replace("\\", "/").rstrip("/")
    if folder != ".":
        for path in spec.get("allowed_files", []):
            normalized = str(path).replace("\\", "/")
            if not normalized.startswith(folder + "/") or ".." in normalized.split("/"):
                problems.append(f"allowed_files path must stay inside {folder}/: {path}")
    for ac in spec.get("acceptance_criteria", []):
        ids = set(ac.get("mvs_ids") or [])
        if not ids:
            continue
        if ids - required:
            problems.append(f"{ac['id']}: unknown MVS ids {sorted(ids - required)}")
        if ac.get("verify_by") != "test" or not ac.get("test_ref") or not ac.get("test_command"):
            problems.append(f"{ac['id']}: MVS requires verify_by=test, test_ref and executable test_command")
        covered |= ids
    if required - covered:
        problems.append(f"Missing MVS coverage: {sorted(required - covered)}")
    return problems


def evidence_problems(
    match: Mapping[str, Any], spec: Mapping[str, Any], evidence: Mapping[str, Any]
) -> list[str]:
    problems = mvs_mapping_problems(match, spec)
    if evidence.get("verdict") != "pass":
        problems.append("Verifier verdict must be pass")
    rows = evidence.get("criteria") or []
    results = {r.get("id"): r for r in rows}
    if len(results) != len(rows):
        problems.append("Duplicate evidence criteria")
    for ac in spec.get("acceptance_criteria", []):
        result = results.get(ac["id"], {})
        if result.get("result") != "pass" or not str(result.get("evidence") or "").strip():
            problems.append(f"{ac['id']}: missing passing evidence")
        if ac.get("mvs_ids") and result.get("evidence_type") != "test_output":
            problems.append(f"{ac['id']}: MVS requires test output evidence")
    regression = evidence.get("regression") or {}
    if not regression.get("ran") or regression.get("result") != "pass":
        problems.append("Regression must have run and passed")
    return problems

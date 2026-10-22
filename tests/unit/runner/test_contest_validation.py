from __future__ import annotations

import copy

import pytest

from digital_development_team.runner.contest_validation import evidence_problems, mvs_mapping_problems

MATCH = {"mvs": [{"id": "MVS-1"}, {"id": "MVS-2"}]}
SPEC = {
    "acceptance_criteria": [
        {
            "id": "AC1",
            "verify_by": "test",
            "mvs_ids": ["MVS-1", "MVS-2"],
            "test_ref": "tests/test_calc.py",
            "test_command": "pytest tests/test_calc.py -q",
        },
    ]
}
EVIDENCE = {
    "verdict": "pass",
    "criteria": [
        {"id": "AC1", "result": "pass", "evidence": "2 tests passed", "evidence_type": "test_output"},
    ],
    "regression": {"ran": True, "result": "pass"},
}


def test_all_mvs_can_share_one_executed_criterion() -> None:
    assert evidence_problems(MATCH, SPEC, EVIDENCE) == []


def test_daily_profile_does_not_enforce_contest_mvs_contract() -> None:
    assert mvs_mapping_problems({}, SPEC) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("mvs_ids", ["MVS-1"]),
        ("mvs_ids", ["MVS-3"]),
        ("verify_by", "manual"),
        ("test_command", ""),
        ("test_ref", ""),
    ],
)
def test_missing_or_unexecutable_mvs_is_rejected(field: str, value: object) -> None:
    spec = copy.deepcopy(SPEC)
    spec["acceptance_criteria"][0][field] = value
    assert mvs_mapping_problems(MATCH, spec)


@pytest.mark.parametrize(
    "field,value",
    [("result", "unclear"), ("result", "fail"), ("evidence", " "), ("evidence_type", "diff_inspection")],
)
def test_verdict_alone_never_overrides_missing_mvs_evidence(field: str, value: str) -> None:
    evidence = copy.deepcopy(EVIDENCE)
    evidence["criteria"][0][field] = value
    assert evidence_problems(MATCH, SPEC, evidence)


def test_regression_and_duplicate_ac_are_rejected() -> None:
    evidence = copy.deepcopy(EVIDENCE)
    evidence["criteria"] *= 2
    evidence["regression"] = {"ran": False, "result": "skipped"}
    assert len(evidence_problems(MATCH, SPEC, evidence)) == 2


def test_allowed_files_must_stay_inside_this_match_folder() -> None:
    match = {"work_folder": "M-1", "mvs": []}
    assert not mvs_mapping_problems(match, {"allowed_files": ["M-1/src/**", "M-1/tests/test.py"]})
    assert mvs_mapping_problems(match, {"allowed_files": ["M-2/src/app.py", "M-1/../secret", "**"]})

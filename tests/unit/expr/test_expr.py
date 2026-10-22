from __future__ import annotations

import pytest

from digital_development_team.core.errors import ExpressionError, MissingValue
from digital_development_team.expr import compile_expr
from digital_development_team.expr.parser import normalize_dialect

SCOPE = {
    "intake": {"type": "bug", "confidence": 0.84, "risk_tier": "T1", "requires_human_before_code": False},
    "spec": {"ambiguities": [{"q": "x"}], "allowed_files": ["src/a.py", "tests/test_a.py"]},
    "plan": {"steps": [{"action": "write_failing_test"}, {"action": "modify"}]},
    "state": {"tier": "T1", "rework": 1, "mg_retries": 0},
    "limits": {"max_rework": 2},
    "files_touched": ["src/auth/session.py", "tests/x.py"],
    "sensitive_paths": ["**/auth/**", "**/migrations/**"],
    "evidence": {"verdict": "pass"},
}


def test_normalize_dialect() -> None:
    assert normalize_dialect("a && b || !c") == "a  and  b  or   not c"
    assert normalize_dialect("x != 1 && y == true") == "x != 1  and  y == True"
    assert normalize_dialect("s == '&& || !'") == "s == '&& || !'"
    assert normalize_dialect("v == null") == "v == None"


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("intake.confidence < 0.6", False),
        ("intake.confidence >= 0.8 and intake.type == 'bug'", True),
        ("intake.type == 'security_incident' || intake.risk_tier == 'T3'", False),
        ("!intake.requires_human_before_code", True),
        ("spec.ambiguities.length > 0 && intake.confidence < 0.7", False),
        ("len(spec.ambiguities) == 1", True),
        ("'modify' in pluck(plan.steps, 'action')", True),
        ("'delete' in pluck(plan.steps, 'action')", False),
        ("state.rework < limits.max_rework", True),
        ("any_match(files_touched, 'migrations/**')", False),
        ("any_match(files_touched, '**/auth/**')", True),
        ("any_in(files_touched, sensitive_paths)", True),
        ("any_in(['a','b'], ['b','c'])", True),
        ("0.5 <= intake.confidence < 0.8", False),
        ("intake.tier == 'T1'", None),  # missing → raise
        ("default(intake.tier, 'T9') == 'T9'", True),
        ("default(state.touches_sensitive, false)", False),
        ("has(intake, 'type') and not has(intake, 'nope')", True),
        ("evidence.verdict in ['pass', 'inconclusive']", True),
        ("spec.allowed_files[0] == 'src/a.py'", True),
        ("startswith(intake.type, 'bu') and contains(spec.allowed_files, 'tests/test_a.py')", True),
        ("-state.rework == -1", True),
    ],
)
def test_eval(src: str, expected: object) -> None:
    e = compile_expr(src)
    if expected is None:
        with pytest.raises(MissingValue):
            e(SCOPE)
    else:
        assert e(SCOPE) == expected, src


def test_names_collected() -> None:
    e = compile_expr("intake.confidence < 0.6 and any_in(files_touched, sensitive_paths)")
    assert e.names == {"intake", "files_touched", "sensitive_paths"}


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "intake.__class__",
        "open('x')",
        "lambda: 1",
        "x = 1",
        "f(a=1)",
        "intake.method()",
        "[i for i in x]",
        "unknown_fn(1)",
        "1 +",
        "default(x)",
        "has(x, y)",
        "{'a': 1}",
    ],
)
def test_rejects(bad: str) -> None:
    with pytest.raises(ExpressionError):
        compile_expr(bad)


def test_missing_is_fail_closed() -> None:
    e = compile_expr("not state.touches_sensitive")
    with pytest.raises(MissingValue):
        e(SCOPE)
    assert compile_expr("not default(state.touches_sensitive, false)")(SCOPE) is True


def test_short_circuit() -> None:
    assert compile_expr("false and missing.thing")(SCOPE) is False
    assert compile_expr("true or missing.thing")(SCOPE) is True

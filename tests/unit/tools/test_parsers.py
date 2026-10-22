from __future__ import annotations

from digital_development_team.tools.parsers import detect_runner, parse_generic, parse_junit, parse_pytest

PYTEST_FAIL = """============================= test session starts =============================
collected 4 items

tests/test_calc.py ..F.                                                  [100%]

================================== FAILURES ===================================
__________________________________ test_mean __________________________________
    def test_mean():
>       assert mean([]) == 0
E       ZeroDivisionError: division by zero
tests/test_calc.py:11: ZeroDivisionError
=========================== short test summary info ===========================
FAILED tests/test_calc.py::test_mean - ZeroDivisionError: division by zero
========================= 1 failed, 3 passed in 0.12s =========================
"""


def test_parse_pytest_fail_and_pass() -> None:
    p = parse_pytest(PYTEST_FAIL, 1)
    assert p["status"] == "fail" and p["passed"] == 3 and p["failed"] == 1
    assert p["summary_line"] == "1 failed, 3 passed in 0.12s"
    assert (
        p["failed_tests"][0]["name"] == "tests/test_calc.py::test_mean"
        and "ZeroDivisionError" in p["failed_tests"][0]["message"]
    )  # type: ignore[index]
    ok = parse_pytest("....\n============ 4 passed in 0.05s ============\n", 0)
    assert ok["status"] == "pass" and ok["passed"] == 4 and ok["duration_sec"] == 0.05
    none = parse_pytest("no tests ran in 0.01s", 5)
    assert none["status"] == "no_tests"
    err = parse_pytest("ImportError while importing test module", 2)
    assert err["status"] == "error"


def test_parse_junit_and_generic() -> None:
    xml = """<testsuites><testsuite name="s" tests="3" failures="1" errors="0" skipped="0" time="0.5">
<testcase classname="tests.test_calc" name="test_add" time="0.1"/>
<testcase classname="tests.test_calc" name="test_mean" time="0.1"><failure message="ZeroDivisionError">trace</failure></testcase>
<testcase classname="tests.test_calc" name="test_median" time="0.1"/>
</testsuite></testsuites>"""
    j = parse_junit(xml)
    assert (
        j["status"] == "fail"
        and j["passed"] == 2
        and j["failed"] == 1
        and j["failed_tests"][0]["name"] == "tests.test_calc::test_mean"
    )  # type: ignore[index]
    assert parse_junit("<bad")["status"] == "error"
    g = parse_generic("jest", "Tests:       1 failed, 12 passed, 13 total\nSnapshots: 0", "", 1)
    assert g["status"] == "fail" and g["passed"] == 12 and g["failed"] == 1
    g2 = parse_generic("go", "ok  \tpkg/calc\t0.012s\n", "", 0)
    assert g2["status"] == "pass" and g2["summary_line"].startswith("ok")
    assert (
        detect_runner("pytest -q") == "pytest"
        and detect_runner("npx jest") == "jest"
        and detect_runner("make check") == "generic"
    )

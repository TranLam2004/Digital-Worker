"""JUnit XML (pytest --junitxml, jest-junit, surefire) → cùng structured với pytest."""

from __future__ import annotations

import xml.etree.ElementTree as ET


def parse_junit(xml_text: str) -> dict[str, object]:
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - xem trên
    except ET.ParseError as e:
        return {
            "runner": "junit",
            "status": "error",
            "error": f"xml hỏng: {e}",
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "failed_tests": [],
            "summary_line": "",
        }
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = failures = errors = skipped = 0
    duration = 0.0
    failed_tests: list[dict[str, object]] = []
    for s in suites:
        tests += int(s.get("tests", 0) or 0)
        failures += int(s.get("failures", 0) or 0)
        errors += int(s.get("errors", 0) or 0)
        skipped += int(s.get("skipped", 0) or 0)
        duration += float(s.get("time", 0) or 0)
        for case in s.iter("testcase"):
            bad = case.find("failure") if case.find("failure") is not None else case.find("error")
            if bad is not None:
                name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
                failed_tests.append(
                    {
                        "name": name,
                        "message": (bad.get("message") or (bad.text or "")).strip()[:300],
                        "file": case.get("file"),
                        "line": case.get("line"),
                    }
                )
    passed = max(tests - failures - errors - skipped, 0)
    status = "pass" if failures == 0 and errors == 0 and tests > 0 else ("no_tests" if tests == 0 else "fail")
    return {
        "runner": "junit",
        "exit_code": 0 if status == "pass" else 1,
        "passed": passed,
        "failed": failures,
        "skipped": skipped,
        "errors": errors,
        "duration_sec": round(duration, 2),
        "failed_tests": failed_tests[:50],
        "summary_line": f"{passed} passed, {failures} failed, {skipped} skipped, {errors} errors in {duration:.1f}s",
        "status": status,
    }

"""pytest: dòng tổng kết `=== 12 passed, 2 failed, 1 skipped in 4.2s ===` + `FAILED path::name - msg`."""

from __future__ import annotations

import re

_SUMMARY_RE = re.compile(
    r"(?P<body>\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)"
    r"(?:, \d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?))*) in (?P<sec>[\d.]+)s"
)
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)")
_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<name>\S+?)(?:\s+-\s+(?P<msg>.*))?$", re.M)
_NO_TESTS_RE = re.compile(r"no tests ran", re.I)
_COLLECT_ERR_RE = re.compile(
    r"(ImportError|ModuleNotFoundError|SyntaxError|collected 0 items|error[s]? during collection)", re.I
)


def parse_pytest(text: str, exit_code: int) -> dict[str, object]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "xfailed": 0, "xpassed": 0}
    summary_line = ""
    duration = 0.0
    matches = list(_SUMMARY_RE.finditer(text))
    if matches:
        m = matches[-1]
        summary_line = f"{m['body']} in {m['sec']}s"
        duration = float(m["sec"])
        for n, kind in _COUNT_RE.findall(m["body"]):
            key = "errors" if kind.startswith("error") else kind
            if key in counts:
                counts[key] += int(n)
    failed_tests = []
    for fm in _FAILED_RE.finditer(text):
        name = fm["name"]
        file, _, _ = name.partition("::")
        failed_tests.append(
            {"name": name, "message": (fm["msg"] or "").strip()[:300], "file": file, "line": None}
        )
    status = "pass" if exit_code == 0 and counts["failed"] == 0 and counts["errors"] == 0 else "fail"
    if exit_code == 5 or _NO_TESTS_RE.search(text):
        status = "no_tests"
    elif exit_code not in (0, 1) and (_COLLECT_ERR_RE.search(text) or not matches):
        status = "error"
    return {
        "runner": "pytest",
        "exit_code": exit_code,
        **counts,
        "duration_sec": duration,
        "failed_tests": failed_tests[:50],
        "summary_line": summary_line,
        "status": status,
    }

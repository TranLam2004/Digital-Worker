"""Runner chưa có parser riêng (jest/go/cargo/mvn): exit code + dòng tổng kết heuristics."""

from __future__ import annotations

import re

_HINTS = (
    re.compile(r"Tests:\s+(?P<body>.*)"),  # jest: Tests: 1 failed, 12 passed, 13 total
    re.compile(r"^(ok|FAIL)\s+\S+\s+[\d.]+s", re.M),  # go test
    re.compile(r"test result: (?P<body>.*)"),  # cargo
    re.compile(r"Tests run: (?P<body>.*)"),  # maven surefire
)
_NUM = re.compile(r"(\d+)\s+(passed|failed|skipped|total|pending|todo)")


def parse_generic(runner: str, stdout: str, stderr: str, exit_code: int) -> dict[str, object]:
    text = stdout + "\n" + stderr
    summary_line = ""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for h in _HINTS:
        m = h.search(text)
        if m:
            summary_line = m.group(0).strip()[:200]
            for n, kind in _NUM.findall(summary_line):
                if kind in counts:
                    counts[kind] = int(n)
            break
    if not summary_line:
        tail = [x for x in text.strip().splitlines() if x.strip()]
        summary_line = tail[-1][:200] if tail else f"exit {exit_code}"
    return {
        "runner": runner,
        "exit_code": exit_code,
        **counts,
        "duration_sec": 0.0,
        "failed_tests": [],
        "summary_line": summary_line,
        "status": "pass" if exit_code == 0 else "fail",
    }

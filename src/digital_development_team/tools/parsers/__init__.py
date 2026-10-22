"""Parse output test runner → structured (passed/failed/failed_tests/summary_line). 0 LLM."""

from __future__ import annotations

import re

from digital_development_team.tools.parsers.generic import parse_generic
from digital_development_team.tools.parsers.junit import parse_junit
from digital_development_team.tools.parsers.langs import (
    parse_dotnet,
    parse_gotest,
    parse_gradle,
    parse_jest,
    parse_js_auto,
    parse_maven,
    parse_node,
)
from digital_development_team.tools.parsers.pytest import parse_pytest

__all__ = [
    "JUNIT_REPORT_DIRS",
    "detect_runner",
    "parse_generic",
    "parse_junit",
    "parse_output",
    "parse_pytest",
]

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpytest\b|python[0-9.]* -m unittest"), "pytest"),
    (re.compile(r"\bnode\b.*--test\b"), "node"),
    (re.compile(r"\bvitest\b"), "vitest"),
    (re.compile(r"\bjest\b"), "jest"),
    (re.compile(r"\bgo test\b"), "go"),
    (re.compile(r"\bcargo test\b"), "cargo"),
    (re.compile(r"\bmvnw?(\.cmd)?\b"), "maven"),
    (re.compile(r"\bgradlew?(\.bat)?\b"), "gradle"),
    (re.compile(r"\bdotnet test\b"), "dotnet"),
    (re.compile(r"\b(npm|pnpm|yarn)( run)? (test|t)\b|\bnpm t\b"), "npm"),
)

JUNIT_REPORT_DIRS: dict[str, tuple[str, ...]] = {
    "maven": ("target/surefire-reports", "target/failsafe-reports"),
    "gradle": ("build/test-results/test", "build/test-results"),
}
"""Runner ghi báo cáo JUnit XML: console `mvn -q` không in số liệu → test.run đọc các file này."""


def detect_runner(cmd: str) -> str:
    c = cmd.lower().replace("\\", "/")
    for pat, runner in _RULES:
        if pat.search(c):
            return runner
    return "generic"


def parse_output(runner: str, stdout: str, stderr: str, exit_code: int) -> dict[str, object]:
    text = stdout + "\n" + stderr
    if runner == "pytest":
        return parse_pytest(text, exit_code)
    if runner == "junit":
        return parse_junit(stdout)
    if runner == "node":
        return parse_node(text, exit_code)
    if runner in ("jest", "vitest"):
        return parse_jest(text, exit_code, runner=runner)
    if runner == "go":
        return parse_gotest(text, exit_code)
    if runner == "maven":
        return parse_maven(text, exit_code)
    if runner == "gradle":
        return parse_gradle(text, exit_code)
    if runner == "dotnet":
        return parse_dotnet(text, exit_code)
    if runner == "npm":
        return parse_js_auto(text, exit_code)
    return parse_generic(runner, stdout, stderr, exit_code)

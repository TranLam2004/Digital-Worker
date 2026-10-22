"""Parser output test cho JS (node:test · jest · vitest) · Go · Maven/Gradle · .NET — cùng structured với pytest.

Chỉ đọc text console (không đổi lệnh của repo để thêm reporter). Maven/Gradle: test.run đọc thêm báo cáo JUnit XML
(target/surefire-reports, build/test-results) khi console không có số liệu (mvn -q).
"""

from __future__ import annotations

import re
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text: str) -> str:
    return _ANSI.sub("", text)


def _result(
    runner: str,
    exit_code: int,
    *,
    passed: int,
    failed: int,
    skipped: int = 0,
    errors: int = 0,
    failed_tests: list[dict[str, Any]],
    summary: str,
    no_tests: bool = False,
    run_error: bool = False,
) -> dict[str, object]:
    if run_error:
        status = "error"
    elif no_tests:
        status = "no_tests"
    elif exit_code == 0 and failed == 0 and errors == 0:
        status = "pass"
    else:
        status = "fail"
    return {
        "runner": runner,
        "exit_code": exit_code,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "duration_sec": 0.0,
        "failed_tests": failed_tests[:50],
        "summary_line": summary[:200],
        "status": status,
    }


def _fail(name: str, message: str = "", file: str | None = None) -> dict[str, Any]:
    return {"name": name.strip(), "message": message.strip()[:300], "file": file, "line": None}


# ───── node --test ─────
_NODE_COUNT = re.compile("^[\u2139#]\\s*(tests|pass|fail|skipped|todo|cancelled)\\s+(\\d+)\\s*$", re.M)
_NODE_FAIL = re.compile(r"^\s*✖\s+(?P<name>.+?)\s+\([\d.]+m?s\)\s*$", re.M)
_TAP_FAIL = re.compile(r"^not ok \d+ - (?P<name>.+)$", re.M)


def parse_node(text: str, exit_code: int) -> dict[str, object]:
    t = _clean(text)
    counts = {k: int(v) for k, v in _NODE_COUNT.findall(t)}
    fails: list[dict[str, Any]] = []
    block = t.split("failing tests:", 1)[1] if "failing tests:" in t else t
    lines = block.splitlines()
    for i, ln in enumerate(lines):
        m = _NODE_FAIL.match(ln) or _TAP_FAIL.match(ln)
        if m and not any(f["name"] == m["name"] for f in fails):
            msg = next((x for x in lines[i + 1 : i + 4] if x.strip() and not x.strip().startswith("at ")), "")
            fails.append(_fail(m["name"], msg))
    total = counts.get("tests", 0)
    failed = counts.get("fail", len(fails))
    return _result(
        "node",
        exit_code,
        passed=counts.get("pass", 0),
        failed=failed,
        skipped=counts.get("skipped", 0),
        failed_tests=fails,
        summary=f"{counts.get('pass', 0)} passed, {failed} failed of {total} (node --test)",
        no_tests=bool(counts) and total == 0,
        run_error=not counts and exit_code != 0,
    )


# ───── jest / vitest ─────
_JEST_TESTS = re.compile(r"^Tests:\s+(?P<body>.+)$", re.M)
_VITEST_TESTS = re.compile(r"^\s*Tests\s+(?P<body>.+?)\s*$", re.M)
_NUM = re.compile(r"(\d+)\s+(passed|failed|skipped|todo|total|pending)")
_JEST_FAIL = re.compile(r"^\s*●\s+(?P<name>.+?)\s*$", re.M)
_VITEST_FAIL = re.compile("^\\s*(?:FAIL|\u00d7|\u2717)\\s+(?P<name>\\S.*?)\\s*(?:\\d+ms)?\\s*$", re.M)


def parse_jest(text: str, exit_code: int, *, runner: str = "jest") -> dict[str, object]:
    t = _clean(text)
    m = _JEST_TESTS.search(t) or _VITEST_TESTS.search(t)
    counts: dict[str, int] = {}
    if m:
        for n, kind in _NUM.findall(m["body"].replace("|", ",")):
            counts[kind] = int(n)
    pat = _JEST_FAIL if "●" in t else _VITEST_FAIL
    fails: list[dict[str, Any]] = []
    for fm in pat.finditer(t):
        name = fm["name"]
        if name.lower().startswith(("console.", "test suites")) or any(f["name"] == name for f in fails):
            continue
        fails.append(_fail(name))
    failed = counts.get("failed", len(fails))
    total = counts.get("total", counts.get("passed", 0) + failed + counts.get("skipped", 0))
    return _result(
        runner,
        exit_code,
        passed=counts.get("passed", 0),
        failed=failed,
        skipped=counts.get("skipped", 0) + counts.get("todo", 0),
        failed_tests=fails,
        summary=(m.group(0).strip() if m else f"exit {exit_code}"),
        no_tests=bool(m) and total == 0,
        run_error=not m and exit_code != 0,
    )


# ───── go test ─────
_GO_FAIL = re.compile(r"^\s*--- FAIL: (?P<name>\S+) \(")
_GO_PASS = re.compile(r"^\s*--- PASS: ")
_GO_SKIP = re.compile(r"^\s*--- SKIP: ")
_GO_PKG = re.compile(r"^(ok|FAIL)\s+(\S+)\s+[\d.]+s", re.M)
_GO_FILE = re.compile(r"(\w+_test\.go):(\d+)")


def parse_gotest(text: str, exit_code: int) -> dict[str, object]:
    t = _clean(text)
    lines = t.splitlines()
    fails: list[dict[str, Any]] = []
    passed = skipped = 0
    for i, ln in enumerate(lines):
        if _GO_PASS.match(ln):
            passed += 1
        elif _GO_SKIP.match(ln):
            skipped += 1
        m = _GO_FAIL.match(ln)
        if m:
            msg = lines[i + 1].strip() if i + 1 < len(lines) else ""
            fm = _GO_FILE.search(msg)
            fails.append(_fail(m["name"], msg, fm.group(1) if fm else None))
    pkgs = _GO_PKG.findall(t)
    build_failed = "[build failed]" in t or "[setup failed]" in t
    ok = sum(1 for s, _ in pkgs if s == "ok")
    bad = sum(1 for s, _ in pkgs if s == "FAIL")
    no_files = "no test files" in t and not pkgs
    summary = (
        f"ok: {ok} package(s), no failures (go test; -v gives per-test counts)"
        if not fails and not bad
        else f"{len(fails)} failed tests, {ok} ok / {bad} FAIL packages (go test)"
    )
    return _result(
        "go",
        exit_code,
        passed=passed,
        failed=len(fails),
        skipped=skipped,
        errors=1 if build_failed else 0,
        failed_tests=fails,
        summary=summary,
        no_tests=no_files,
        run_error=build_failed or (exit_code != 0 and not fails and not bad),
    )


# ───── Maven (surefire) / Gradle ─────
_MVN_RUN = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)(?!.*-- in)")
_MVN_FAIL = re.compile(r"^\[ERROR\]\s{3}(?P<name>[\w$.]+[.:#]\w+)(?::\d+)?\s*(?P<msg>.*)$", re.M)
_GRADLE_SUM = re.compile(r"(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?")
_GRADLE_FAIL = re.compile(r"^(?P<cls>\S+) > (?P<name>.+?) FAILED$", re.M)


def parse_maven(text: str, exit_code: int) -> dict[str, object]:
    t = _clean(text)
    runs = _MVN_RUN.findall(t)
    fails = [_fail(m["name"], m["msg"]) for m in _MVN_FAIL.finditer(t)]
    if runs:
        total, f, e, s = (int(x) for x in runs[-1])
        return _result(
            "maven",
            exit_code,
            passed=max(total - f - e - s, 0),
            failed=f,
            skipped=s,
            errors=e,
            failed_tests=fails,
            summary=f"Tests run: {total}, Failures: {f}, Errors: {e}, Skipped: {s}",
            no_tests=total == 0,
        )
    compile_err = "COMPILATION ERROR" in t or ("BUILD FAILURE" in t and not fails)
    return _result(
        "maven",
        exit_code,
        passed=0,
        failed=len(fails),
        failed_tests=fails,
        summary="no test summary in output" if exit_code == 0 else "build failed before tests",
        run_error=exit_code != 0 and compile_err,
    )


def parse_gradle(text: str, exit_code: int) -> dict[str, object]:
    t = _clean(text)
    m = _GRADLE_SUM.search(t)
    fails = [_fail(f"{fm['cls']}::{fm['name']}") for fm in _GRADLE_FAIL.finditer(t)]
    if m:
        total, f = int(m.group(1)), int(m.group(2))
        s = int(m.group(3) or 0)
        return _result(
            "gradle",
            exit_code,
            passed=total - f - s,
            failed=f,
            skipped=s,
            failed_tests=fails,
            summary=m.group(0),
        )
    return _result(
        "gradle",
        exit_code,
        passed=0,
        failed=len(fails),
        failed_tests=fails,
        summary="BUILD SUCCESSFUL" if exit_code == 0 else "build failed",
        run_error=exit_code != 0 and not fails,
    )


# ───── dotnet test ─────
_DN_VSTEST = re.compile(
    r"(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+),\s+Skipped:\s+(\d+),\s+Total:\s+(\d+)"
)
_DN_SUMMARY = re.compile(
    r"total:\s*(\d+),?\s*failed:\s*(\d+),?\s*succeeded:\s*(\d+),?\s*skipped:\s*(\d+)", re.I
)
_DN_FAIL = re.compile(r"^\s*Failed (?P<name>[\w.]+(?:\(.*?\))?) \[[^\]]+\]\s*$", re.M)


def parse_dotnet(text: str, exit_code: int) -> dict[str, object]:
    t = _clean(text)
    lines = t.splitlines()
    fails: list[dict[str, Any]] = []
    for i, ln in enumerate(lines):
        m = _DN_FAIL.match(ln)
        if m:
            msg = ""
            for nxt in lines[i + 1 : i + 4]:
                if nxt.strip() and "Error Message" not in nxt:
                    msg = nxt
                    break
            fails.append(_fail(m["name"], msg))
    f = p = s = total = 0
    hits = _DN_VSTEST.findall(t)
    if hits:
        for hf, hp, hs, ht in hits:  # một dòng mỗi project test
            f, p, s, total = f + int(hf), p + int(hp), s + int(hs), total + int(ht)
    else:
        m2 = _DN_SUMMARY.search(t)
        if m2:
            total, f, p, s = (int(x) for x in m2.groups())
    found = bool(hits) or bool(_DN_SUMMARY.search(t))
    return _result(
        "dotnet",
        exit_code,
        passed=p,
        failed=f,
        skipped=s,
        failed_tests=fails,
        summary=f"{p} passed, {f} failed, {s} skipped of {total} (dotnet test)"
        if found
        else f"exit {exit_code}",
        no_tests=found and total == 0,
        run_error=not found and exit_code != 0,
    )


def parse_js_auto(text: str, exit_code: int) -> dict[str, object]:
    """npm/yarn/pnpm test: script bên trong là gì thì nhận diện theo output."""
    t = _clean(text)
    if re.search("^[\u2139#] tests \\d+", t, re.M):
        return parse_node(t, exit_code)
    if _JEST_TESTS.search(t):
        return parse_jest(t, exit_code)
    if _VITEST_TESTS.search(t):
        return parse_jest(t, exit_code, runner="vitest")
    return _result(
        "npm",
        exit_code,
        passed=0,
        failed=0,
        failed_tests=[],
        summary=next((x for x in reversed(t.splitlines()) if x.strip()), f"exit {exit_code}")[:200],
        run_error=False,
    )

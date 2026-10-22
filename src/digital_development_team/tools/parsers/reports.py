"""Báo cáo JUnit XML do runner ghi ra (surefire, gradle) → structured, khi console không có số liệu (mvn -q)."""

from __future__ import annotations

from pathlib import Path

from digital_development_team.tools.parsers.junit import parse_junit


def read_junit_reports(root: Path, dirs: tuple[str, ...], *, since: float) -> dict[str, object] | None:
    """Gộp mọi TEST-*.xml mới hơn `since` (epoch giây) trong các thư mục báo cáo. Không có → None."""
    passed = failed = skipped = errors = 0
    failed_tests: list[object] = []
    found = 0
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for xml in sorted(base.rglob("TEST-*.xml")):
            try:
                if xml.stat().st_mtime < since:
                    continue  # báo cáo cũ của lần chạy trước
                r = parse_junit(xml.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if r.get("status") == "error":
                continue
            found += 1
            passed += int(r["passed"])  # type: ignore[call-overload]
            failed += int(r["failed"])  # type: ignore[call-overload]
            skipped += int(r["skipped"])  # type: ignore[call-overload]
            errors += int(r["errors"])  # type: ignore[call-overload]
            failed_tests += list(r.get("failed_tests") or [])  # type: ignore[call-overload]
    if not found:
        return None
    total = passed + failed + skipped + errors
    status = "pass" if failed == 0 and errors == 0 and total > 0 else ("no_tests" if total == 0 else "fail")
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "failed_tests": failed_tests[:50],
        "summary_line": f"{passed} passed, {failed} failed, {skipped} skipped, {errors} errors ({found} JUnit reports)",
        "status": status,
        "reports": found,
    }

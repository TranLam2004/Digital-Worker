"""Báo cáo sự cố bảo mật: advisory, 5C, finding theo mức, việc cần làm ngay; CLI."""

from __future__ import annotations

from digital_development_team.app import Runtime
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.reporting.security import build_security_report
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.stub import stub_handlers
from tests.conftest import TESTDATA

ISSUE = "DDT-0001"
INCIDENT = {
    "contain": {"status": "applied", "measure": "tắt endpoint search bằng feature flag"},
    "confirm": {"poc_exists": True, "poc_ref": "tests/test_sqli.py", "exploited_evidence": "suspected"},
    "correct": {
        "variant_hunt_done": True,
        "variants_found": ["api/admin.py:30", "api/search.py:12"],
        "variants_fixed_in_this_patch": True,
    },
    "check": {"poc_no_longer_reproduces": True, "regression_pass": True},
    "communicate": {
        "advisory_ref": "SA-2026-01",
        "secret_rotation_needed": True,
        "user_notification_needed": False,
    },
}
FINDINGS = [
    {
        "severity": "low",
        "cwe_or_category": "A09",
        "location": "api/log.py:10",
        "description": "log thiếu ngữ cảnh",
    },
    {
        "severity": "high",
        "cwe_or_category": "CWE-89",
        "location": "api/users.py:42",
        "description": "ghép chuỗi vào SQL",
        "remediation": "parameterized query",
    },
]


def _run(rt: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    ov = {"sec_report": {"mode": "incident_5c", "findings": FINDINGS, "incident_5c": INCIDENT}}
    reg = default_handlers(stub=True)
    reg = StageRegistry({**{s: reg.get(s) for s in reg.stages()}, **stub_handlers(ov)})
    Runner(rt.services(with_agents=False), handlers=reg, stub=True).run(ISSUE, cancel=CancelToken())


def test_security_report_sections(runtime: Runtime, fake_clock: FakeClock) -> None:
    _run(runtime)
    r = build_security_report(runtime.layout, runtime.db, runtime.store, ISSUE, now=fake_clock.now())
    md = r.markdown
    assert r.severity == "high" and r.incident is True
    assert "## Advisory" in md and "ghép chuỗi vào SQL" in md.split("## 5C")[0]  # finding nặng nhất lên đầu
    assert (
        "Contain (khoanh vùng):** status: applied" in md
        and "variants_found: api/admin.py:30, api/search.py:12" in md
    )
    assert md.index("| high |") < md.index("| low |")
    assert "Xoay secret" in md and "dấu vết khai thác" in md and "## Dòng thời gian" in md


def test_security_report_cli(runtime: Runtime, fake_clock: FakeClock) -> None:
    from typer.testing import CliRunner

    from digital_development_team.cli.main import app

    _run(runtime)
    r = CliRunner().invoke(app, ["--home", str(runtime.home), "security-report", ISSUE])
    assert r.exit_code == 0 and "Advisory" in r.output

"""Phase 6: parser test cho node --test · jest · vitest · go test · Maven (console + JUnit XML) · Gradle · dotnet.

Fixture go/node/mvn/surefire là output THẬT (đã bỏ đường dẫn máy); jest/vitest/gradle/dotnet theo định dạng chuẩn.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from digital_development_team.tools.parsers import JUNIT_REPORT_DIRS, detect_runner, parse_output
from digital_development_team.tools.parsers.reports import read_junit_reports

FIX = Path(__file__).parent / "fixtures" / "langs"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("cmd", "runner"),
    [
        ("pytest -q", "pytest"),
        ("node --test", "node"),
        ("npx jest --ci", "jest"),
        ("pnpm vitest run", "vitest"),
        ("go test ./...", "go"),
        ("mvn -q -o test", "maven"),
        ("./mvnw test", "maven"),
        ("gradlew.bat test", "gradle"),
        ("dotnet test", "dotnet"),
        ("yarn test", "npm"),
        ("pnpm run test", "npm"),
        ("make lint", "generic"),
    ],
)
def test_detect_runner(cmd: str, runner: str) -> None:
    assert detect_runner(cmd) == runner


def test_node_go_maven_real_outputs() -> None:
    node = parse_output("node", _read("node_fail.txt"), "", 1)
    assert (node["status"], node["passed"], node["failed"]) == ("fail", 4, 1)
    assert node["failed_tests"][0]["name"] == "reserve over stock"  # type: ignore[index]
    assert "Missing expected exception" in node["failed_tests"][0]["message"]  # type: ignore[index]

    go = parse_output("go", _read("go_fail.txt"), "", 1)
    assert go["status"] == "fail" and go["failed"] == 1
    assert go["failed_tests"][0]["name"] == "TestReserveOverStock"  # type: ignore[index]
    assert go["failed_tests"][0]["file"] == "shop_test.go"  # type: ignore[index]
    gov = parse_output("go", _read("go_fail_v.txt"), "", 1)
    assert gov["passed"] == 4 and gov["failed"] == 1

    mvn = parse_output("maven", _read("mvn_fail.txt"), "", 1)
    assert (mvn["status"], mvn["passed"], mvn["failed"]) == ("fail", 3, 1)
    assert mvn["failed_tests"][0]["name"].startswith("ShopTest.reserveOverStock")  # type: ignore[index]
    q = parse_output("maven", _read("mvn_fail_q.txt"), "", 1)
    assert q["failed"] == 1 and q["status"] == "fail"


def test_js_frameworks_gradle_dotnet() -> None:
    jest = parse_output("jest", _read("jest_fail.txt"), "", 1)
    assert (jest["passed"], jest["failed"]) == (3, 1)
    assert jest["failed_tests"][0]["name"] == "Inventory › reserve over stock"  # type: ignore[index]  # noqa: RUF001
    vit = parse_output("vitest", _read("vitest_fail.txt"), "", 1)
    assert (vit["passed"], vit["failed"]) == (3, 1) and vit["failed_tests"]
    auto = parse_output("npm", _read("jest_fail.txt"), "", 1)
    assert auto["runner"] == "jest" and auto["failed"] == 1  # npm test → nhận diện jest theo output
    gr = parse_output("gradle", _read("gradle_fail.txt"), "", 1)
    assert (gr["passed"], gr["failed"]) == (3, 1) and gr["failed_tests"][0][
        "name"
    ] == "ShopTest::reserveOverStock()"  # type: ignore[index]
    dn = parse_output("dotnet", _read("dotnet_fail.txt"), "", 1)
    assert (dn["passed"], dn["failed"]) == (3, 1)
    assert dn["failed_tests"][0]["name"] == "Shop.Tests.InventoryTests.ReserveOverStock"  # type: ignore[index]
    assert "No exception was thrown" in dn["failed_tests"][0]["message"]  # type: ignore[index]


def test_junit_reports_fill_in_quiet_maven(tmp_path: Path) -> None:
    rep_dir = tmp_path / "target" / "surefire-reports"
    rep_dir.mkdir(parents=True)
    shutil.copy(FIX / "surefire_TEST-shop.ShopTest.xml", rep_dir / "TEST-shop.ShopTest.xml")
    rep = read_junit_reports(tmp_path, JUNIT_REPORT_DIRS["maven"], since=time.time() - 60)
    assert rep is not None and (rep["passed"], rep["failed"], rep["status"]) == (3, 1, "fail")
    old = time.time() - 3600
    os.utime(rep_dir / "TEST-shop.ShopTest.xml", (old, old))
    assert (
        read_junit_reports(tmp_path, JUNIT_REPORT_DIRS["maven"], since=time.time() - 60) is None
    )  # báo cáo cũ

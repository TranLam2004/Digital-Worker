"""Phase 6: pkg.install / pkg.add / pkg.restore — tool tự dựng lệnh, kiểm ghim phiên bản · typosquat · tuổi · OSV."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from digital_development_team.config.models import NetworkCfg, SandboxCfg
from digital_development_team.core.context import Actor, CancelToken, SystemClock, ToolContext
from digital_development_team.core.enums import Stage
from digital_development_team.sandbox.env import build_env
from digital_development_team.tools.pkg import (
    PkgTools,
    _exclude,
    install_command,
    restore_commands,
    typosquat_of,
)
from tests.unit.tools.test_tools import env  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 11, tzinfo=UTC)


class _Pkg(PkgTools):
    """Không chạy lệnh thật: ghi lại lệnh harness dựng."""

    def __init__(self, net: NetworkCfg, fetch: Any) -> None:
        super().__init__(None, SandboxCfg(), net, fetch=fetch, now=lambda: NOW)  # type: ignore[arg-type]
        self.cmds: list[str] = []

    def _run(self, cmd: str, wt: Path, ctx: ToolContext) -> dict[str, Any]:
        self.cmds.append(cmd)
        return {"exit_code": 0, "tail": "", "duration_ms": 1}


def _fetch(vulns: list[str] | None = None, age_days: int = 400, osv_down: bool = False) -> Any:
    def f(method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any] | None:
        if "osv.dev" in url:
            return None if osv_down else {"vulns": [{"id": v} for v in vulns or []]}
        ts = (NOW - timedelta(days=age_days)).isoformat()
        if "pypi.org" in url:
            return {"urls": [{"upload_time_iso_8601": ts}]}
        if "npmjs.org" in url:
            return {"time": {"1.2.3": ts, "4.17.21": ts}}
        return None

    return f


def _ctx(wt: Path) -> ToolContext:
    return ToolContext(
        issue_id="DDT-0001",
        actor=Actor(role="dev"),
        stage=Stage.IMPLEMENT,
        worktree=str(wt),
        home=".",
        allowed_files=(),
        cancel=CancelToken(),
        clock=SystemClock(),
    )


@pytest.mark.parametrize(
    "spec",
    [
        "requests",
        "requests>=2",
        "requests@^2.0.0",
        "git+https://x/y@1.0.0",
        "../evil@1.0.0",
        "https://x@1.0.0",
    ],
)
def test_specs_must_be_exact_pins(tmp_path: Path, spec: str) -> None:
    p = _Pkg(NetworkCfg(), _fetch())
    r = p.install({"ecosystem": "pypi", "packages": [spec], "reason": "x"}, _ctx(tmp_path), None)
    assert r.status == "denied" and r.structured["rule"] == "pkg:spec" and not p.cmds


def test_checks_typosquat_malicious_age_and_vulns(tmp_path: Path) -> None:
    assert typosquat_of("pypi", "requets") == "requests" and typosquat_of("pypi", "requests") is None
    deny = lambda fetch, spec="tabulate@0.9.0": _Pkg(NetworkCfg(), fetch).install(  # noqa: E731
        {"ecosystem": "pypi", "packages": [spec], "reason": "x"}, _ctx(tmp_path), None
    )
    assert deny(_fetch(), "requets@2.0.0").structured["rule"] == "pkg:typosquat"
    assert deny(_fetch(vulns=["MAL-2026-1"])).structured["rule"] == "pkg:malicious"
    assert deny(_fetch(age_days=2)).structured["rule"] == "pkg:too_new"
    ok = deny(_fetch(vulns=["GHSA-1234"]))
    assert ok.status == "ok" and ok.structured["checks"]["tabulate@0.9.0"]["vulns"] == ["GHSA-1234"]
    soft = deny(_fetch(osv_down=True))
    assert soft.status == "ok" and "osv" in soft.structured["checks"]["tabulate@0.9.0"]["unverified"]
    strict = _Pkg(NetworkCfg(pkg_checks="required"), _fetch(osv_down=True)).install(
        {"ecosystem": "pypi", "packages": ["tabulate@0.9.0"], "reason": "x"}, _ctx(tmp_path), None
    )
    assert strict.status == "denied" and strict.structured["rule"] == "pkg:unverified"


def test_commands_never_run_install_scripts(tmp_path: Path) -> None:
    pip = install_command("pypi", [("tabulate", "0.9.0")], save=False, wt=tmp_path)
    assert pip is not None and "--only-binary :all:" in pip and "--target .ddt/site" in pip
    npm = install_command("npm", [("@scope/lib", "1.2.3")], save=False, wt=tmp_path)
    assert npm == "npm install --no-save --ignore-scripts @scope/lib@1.2.3"
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert install_command("npm", [("lodash", "4.17.21")], save=True, wt=tmp_path) == (
        "pnpm add --save-exact --ignore-scripts lodash@4.17.21"
    )
    assert install_command("go", [("github.com/google/uuid", "v1.6.0")], save=True, wt=tmp_path) == (
        "go get github.com/google/uuid@v1.6.0"
    )
    (tmp_path / "App.csproj").write_text("<Project/>", encoding="utf-8")
    assert install_command("nuget", [("Serilog", "4.0.0")], save=True, wt=tmp_path) == (
        "dotnet add App.csproj package Serilog --version 4.0.0"
    )
    p = _Pkg(NetworkCfg(), _fetch())
    r = p.install({"ecosystem": "npm", "packages": ["@scope/lib@1.2.3"], "reason": "x"}, _ctx(tmp_path), None)
    assert r.status == "ok" and p.cmds == ["pnpm add --no-save --ignore-scripts @scope/lib@1.2.3"]
    go = p.install(
        {"ecosystem": "go", "packages": ["github.com/google/uuid@v1.6.0"], "reason": "x"},
        _ctx(tmp_path),
        None,
    )
    assert go.status == "error" and "pkg.add" in (go.error.message if go.error else "")


def test_restore_and_exclude_and_pythonpath(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    for f in ("package-lock.json", "requirements.txt", "go.mod"):
        (wt / f).write_text("", encoding="utf-8")
    cmds = restore_commands(wt)
    assert (
        cmds[0] == "npm ci --ignore-scripts"
        and "go mod download" in cmds
        and "-r requirements.txt" in cmds[1]
    )
    # worktree: .git là file trỏ về <repo>/.git/worktrees/<name> → exclude ghi vào <repo>/.git/info/exclude
    common = tmp_path / "repo" / ".git"
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    _exclude(wt, (".ddt/", "node_modules/"))
    _exclude(wt, (".ddt/",))
    assert (common / "info" / "exclude").read_text(encoding="utf-8").splitlines() == [
        ".ddt/",
        "node_modules/",
    ]
    assert "site" not in build_env(worktree=str(wt), host={}).get("PYTHONPATH", "")
    (wt / ".ddt" / "site").mkdir(parents=True)
    assert build_env(worktree=str(wt), host={})["PYTHONPATH"].endswith("site")


def test_pkg_add_needs_g2_and_verifier_has_no_pkg(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.unit.tools.test_tools import _dispatch

    rt, tooling, agents, wt = env
    r = _dispatch(
        rt, tooling, agents, wt, "dev", "pkg.add", ecosystem="npm", packages=["lodash@4.17.21"], reason="x"
    )
    assert r.status == "denied" and "G2" in r.summary and r.structured.get("approval_key")
    r = _dispatch(
        rt, tooling, agents, wt, "verifier", "pkg.install", ecosystem="pypi", packages=["x@1.0"], reason="x"
    )
    assert r.status == "denied"

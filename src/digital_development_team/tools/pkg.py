"""pkg.install · pkg.add · pkg.restore — cài gói từ registry chính thức (user chốt 2026-10-11), chỉ Dev.

Agent KHÔNG đưa lệnh: tool tự dựng lệnh từ (ecosystem, name@version). Kiểm trước khi cài:
  dạng      tên hợp lệ, phiên bản CHÍNH XÁC; cấm URL · đường dẫn · git+ · file: · tham số lạ
  typosquat tên cách tên gói phổ biến đúng 1 ký tự → từ chối
  tuổi      bản phát hành < `network.pkg_min_age_days` ngày → từ chối (bản độc thường bị gỡ trong vài ngày)
  OSV       có mã MAL- (gói độc) → từ chối; có lỗ hổng đã biết → cài nhưng báo
Không kiểm được (mạng) → `network.pkg_checks`: best_effort cài kèm cảnh báo · required từ chối.

install  KHÔNG sửa manifest: pypi → `--target <worktree>/.ddt/site` (sandbox thêm vào PYTHONPATH) · npm/yarn/pnpm → --no-save.
add      sửa manifest (npm --save-exact · go get · dotnet add package) → policy: G2 người duyệt.
restore  chạy bước restore của SETUP.yaml / mặc định theo stack (npm ci --ignore-scripts, go mod download…).
Mọi lệnh tắt install script (--ignore-scripts, --only-binary :all:) và chạy qua sandbox.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from digital_development_team.config.models import NetworkCfg, SandboxCfg
from digital_development_team.core.context import ToolContext
from digital_development_team.core.errors import SandboxError
from digital_development_team.core.models import ToolResult
from digital_development_team.core.protocols import Sandbox
from digital_development_team.policy.netguard import check_url
from digital_development_team.sandbox.profiles import profile_for
from digital_development_team.tools.base import Handler
from digital_development_team.tools.exec import read_output
from digital_development_team.tools.web import tls_context

SITE_DIR = ".ddt/site"
ECOSYSTEMS = ("pypi", "npm", "go", "nuget")
_OSV_ECO = {"pypi": "PyPI", "npm": "npm", "go": "Go", "nuget": "NuGet"}
_NAME = {
    "pypi": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
    "npm": re.compile(r"^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]{0,213}$"),
    "go": re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(/[A-Za-z0-9._~-]+)+$"),
    "nuget": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
}
_VERSION = {
    "pypi": re.compile(r"^[0-9][0-9A-Za-z.!+-]{0,49}$"),
    "npm": re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"),
    "go": re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"),
    "nuget": re.compile(r"^\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z.-]+)?$"),
}
POPULAR = {
    "pypi": (
        "requests",
        "numpy",
        "pandas",
        "django",
        "flask",
        "fastapi",
        "pydantic",
        "pytest",
        "urllib3",
        "boto3",
        "sqlalchemy",
        "httpx",
        "jinja2",
        "pyyaml",
        "click",
        "rich",
        "typer",
        "scipy",
        "matplotlib",
        "pillow",
        "cryptography",
        "setuptools",
        "attrs",
        "aiohttp",
        "celery",
        "redis",
        "psycopg2",
        "beautifulsoup4",
        "lxml",
    ),
    "npm": (
        "react",
        "lodash",
        "express",
        "axios",
        "chalk",
        "commander",
        "debug",
        "moment",
        "uuid",
        "vue",
        "jest",
        "typescript",
        "webpack",
        "eslint",
        "prettier",
        "dotenv",
        "yargs",
        "rxjs",
        "next",
        "vite",
        "vitest",
        "zod",
        "dayjs",
        "cors",
        "body-parser",
        "mongoose",
        "jsonwebtoken",
        "bcrypt",
        "nodemon",
        "socket.io",
    ),
    "go": (
        "github.com/stretchr/testify",
        "github.com/google/uuid",
        "github.com/spf13/cobra",
        "github.com/gin-gonic/gin",
    ),
    "nuget": (
        "Newtonsoft.Json",
        "Serilog",
        "xunit",
        "NUnit",
        "Moq",
        "AutoMapper",
        "Dapper",
        "FluentAssertions",
    ),
}

Fetch = Callable[[str, str, dict[str, Any] | None], dict[str, Any] | None]
"""(method, url, json_body) → JSON hoặc None khi không gọi được. Test tiêm bản giả."""


def _edit1(a: str, b: str) -> bool:
    """Khoảng cách sửa đúng 1 (thay/thêm/bớt một ký tự)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    s, t = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(s) and s[i] == t[i]:
        i += 1
    return s[i:] == t[i + 1 :]


def typosquat_of(eco: str, name: str) -> str | None:
    low = name.lower()
    pop = [p.lower() for p in POPULAR.get(eco, ())]
    if low in pop:
        return None
    return next((p for p in pop if _edit1(low, p)), None)


class PkgTools:
    def __init__(
        self,
        sandbox: Sandbox,
        cfg: SandboxCfg,
        net: NetworkCfg,
        *,
        fetch: Fetch | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sandbox = sandbox
        self._cfg = cfg
        self._net = net
        self._fetch = fetch or self._http
        self._now = now

    def handlers(self) -> dict[str, Handler]:
        return {"install": self.install, "add": self.add, "restore": self.restore}

    # ───── ops ─────
    def install(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        return self._do(args, ctx, save=False)

    def add(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        return self._do(args, ctx, save=True)

    def restore(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if not ctx.worktree:
            return ToolResult.fail("NO_WORKTREE", "stage này không có worktree")
        wt = Path(ctx.worktree)
        cmds = restore_commands(wt)
        if not cmds:
            return ToolResult.ok("pkg.restore → nothing to restore", commands=[])
        _exclude(wt, (".ddt/", "node_modules/"))
        results = []
        for cmd in cmds:
            r = self._run(cmd, wt, ctx)
            results.append({"cmd": cmd, **r})
            if r["exit_code"] != 0:
                return ToolResult.fail(
                    "RESTORE_FAILED", f"`{cmd}` exit {r['exit_code']}: {r['tail']}", results=results
                )
        return ToolResult.ok(f"pkg.restore → {len(results)} step(s) ok", results=results)

    # ───── nội bộ ─────
    def _do(self, args: Mapping[str, Any], ctx: ToolContext, *, save: bool) -> ToolResult:
        if not ctx.worktree:
            return ToolResult.fail("NO_WORKTREE", "stage này không có worktree")
        wt = Path(ctx.worktree)
        eco = str(args.get("ecosystem") or "")
        if eco not in ECOSYSTEMS:
            return ToolResult.fail("BAD_ARGS", f"ecosystem must be one of {ECOSYSTEMS}")
        if not save and eco in ("go", "nuget"):
            return ToolResult.fail("BAD_ARGS", f"{eco} packages always change the manifest: use pkg.add (G2)")
        specs = [str(p).strip() for p in args.get("packages") or [] if str(p).strip()]
        if not specs or len(specs) > 5:
            return ToolResult.fail("BAD_ARGS", "packages: 1 to 5 entries `name@exact-version`")
        parsed: list[tuple[str, str]] = []
        for spec in specs:
            name, sep, version = (
                spec.rpartition("@")
                if spec.count("@") > (1 if spec.startswith("@") else 0)
                else (spec, "", "")
            )
            if not sep or not _NAME[eco].match(name) or not _VERSION[eco].match(version):
                return ToolResult.denied(
                    "pkg:spec",
                    f"`{spec}` is not `name@exact-version` for {eco} (no ranges, URLs, paths or git refs)",
                    hint="Pin one exact version, e.g. requests@2.32.3",
                )
            squat = typosquat_of(eco, name)
            if squat:
                return ToolResult.denied(
                    "pkg:typosquat", f"`{name}` is one letter away from the popular `{squat}`"
                )
            parsed.append((name, version))
        checks: dict[str, Any] = {}
        for name, version in parsed:
            verdict = self._verify(eco, name, version)
            checks[f"{name}@{version}"] = verdict
            if verdict.get("deny"):
                return ToolResult.denied(f"pkg:{verdict['deny']}", str(verdict.get("reason")), checks=checks)
        cmd = install_command(eco, parsed, save=save, wt=wt, project=str(args.get("project") or ""))
        if cmd is None:
            return ToolResult.fail("BAD_ARGS", f"cannot build an install command for {eco} here")
        _exclude(wt, (".ddt/", "node_modules/"))
        r = self._run(cmd, wt, ctx)
        if r["exit_code"] != 0:
            return ToolResult.fail(
                "INSTALL_FAILED", f"exit {r['exit_code']}: {r['tail']}", cmd=cmd, checks=checks
            )
        warn = [k for k, v in checks.items() if v.get("vulns") or v.get("unverified")]
        return ToolResult.ok(
            f"pkg.{'add' if save else 'install'} {', '.join(specs)} → ok"
            + (f" (check: {', '.join(warn)})" if warn else ""),
            cmd=cmd,
            checks=checks,
            site=SITE_DIR if eco == "pypi" else None,
        )

    def _verify(self, eco: str, name: str, version: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        osv = self._fetch(
            "POST",
            "https://api.osv.dev/v1/query",
            {"package": {"name": name, "ecosystem": _OSV_ECO[eco]}, "version": version},
        )
        if osv is None:
            out["unverified"] = ["osv"]
        else:
            ids = [str(v.get("id")) for v in osv.get("vulns") or [] if isinstance(v, dict)]
            if any(i.startswith("MAL-") for i in ids):
                return {"deny": "malicious", "reason": f"OSV lists {name}@{version} as malicious: {ids[:3]}"}
            if ids:
                out["vulns"] = ids[:10]
        published = self._published(eco, name, version)
        if published is None:
            out.setdefault("unverified", []).append("age")
        else:
            age = (self._now() - published).days
            out["age_days"] = age
            if age < self._net.pkg_min_age_days:
                return {
                    "deny": "too_new",
                    "reason": f"{name}@{version} was published {age} day(s) ago (< {self._net.pkg_min_age_days})",
                }
        if out.get("unverified") and self._net.pkg_checks == "required":
            return {"deny": "unverified", "reason": f"could not verify {name}@{version}: {out['unverified']}"}
        return out

    def _published(self, eco: str, name: str, version: str) -> datetime | None:
        try:
            if eco == "pypi":
                d = self._fetch("GET", f"https://pypi.org/pypi/{name}/{version}/json", None)
                urls = (d or {}).get("urls") or []
                ts = urls[0].get("upload_time_iso_8601") if urls else None
            elif eco == "npm":
                d = self._fetch("GET", f"https://registry.npmjs.org/{name}", None)
                ts = ((d or {}).get("time") or {}).get(version)
            else:
                return None
        except (AttributeError, IndexError, TypeError):
            return None
        if not ts:
            return None
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _http(self, method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any] | None:
        if not check_url(url, self._net).ok:
            return None
        try:
            with httpx.Client(verify=tls_context(), timeout=self._net.timeout_s) as c:
                r = c.request(method, url, json=body) if body is not None else c.request(method, url)
            if r.status_code >= 400:
                return None
            d = r.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    def _run(self, cmd: str, wt: Path, ctx: ToolContext) -> dict[str, Any]:
        try:
            prof = profile_for(self._cfg, "dev", wt)
            res = self._sandbox.run(cmd, cwd=wt, profile=prof, timeout_s=600.0, cancel=ctx.cancel)
        except SandboxError as e:
            return {"exit_code": -1, "tail": e.message[:300]}
        stdout, stderr = read_output(res)
        tail = next((ln for ln in reversed((stderr or stdout).splitlines()) if ln.strip()), "")
        return {"exit_code": res.exit_code, "tail": tail[:300], "duration_ms": res.duration_ms}


def install_command(
    eco: str, pkgs: list[tuple[str, str]], *, save: bool, wt: Path, project: str = ""
) -> str | None:
    q = shlex.quote
    if eco == "pypi":
        reqs = " ".join(q(f"{n}=={v}") for n, v in pkgs)
        return f"python -m pip install --disable-pip-version-check --no-input --only-binary :all: --target {SITE_DIR} {reqs}"
    if eco == "npm":
        specs = " ".join(q(f"{n}@{v}") for n, v in pkgs)
        if (wt / "pnpm-lock.yaml").is_file():
            return f"pnpm add {'--save-exact' if save else '--no-save'} --ignore-scripts {specs}"
        if (wt / "yarn.lock").is_file() and save:
            return f"yarn add --exact --ignore-scripts {specs}"
        return f"npm install {'--save-exact' if save else '--no-save'} --ignore-scripts {specs}"
    if eco == "go":
        return "go get " + " ".join(q(f"{n}@{v}") for n, v in pkgs)
    if eco == "nuget":
        proj = project or next(
            (str(p.relative_to(wt)).replace("\\", "/") for p in sorted(wt.rglob("*.csproj"))), ""
        )
        if not proj or len(pkgs) != 1:
            return None
        n, v = pkgs[0]
        return f"dotnet add {q(proj)} package {q(n)} --version {q(v)}"
    return None


def restore_commands(wt: Path) -> list[str]:
    out: list[str] = []
    if (wt / "package-lock.json").is_file():
        out.append("npm ci --ignore-scripts")
    elif (wt / "pnpm-lock.yaml").is_file():
        out.append("pnpm install --frozen-lockfile --ignore-scripts")
    elif (wt / "yarn.lock").is_file():
        out.append("yarn install --frozen-lockfile --ignore-scripts")
    if (wt / "requirements.txt").is_file():
        out.append(
            f"python -m pip install --disable-pip-version-check --no-input --only-binary :all: --target {SITE_DIR} -r requirements.txt"
        )
    if (wt / "go.mod").is_file():
        out.append("go mod download")
    if (wt / "pom.xml").is_file():
        out.append("mvn -q dependency:go-offline")
    if any(wt.glob("*.sln")) or any(wt.rglob("*.csproj")):
        out.append("dotnet restore")
    return out


def _exclude(wt: Path, patterns: tuple[str, ...]) -> None:
    """Thư mục cài đặt không được tính là file Dev sửa (scope) — ghi vào info/exclude của repo (worktree dùng chung)."""
    gitfile = wt / ".git"
    try:
        if gitfile.is_file():  # worktree: ".git" là file trỏ về gitdir của worktree
            gitdir = Path(gitfile.read_text(encoding="utf-8").split(":", 1)[1].strip())
            common = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
        elif gitfile.is_dir():
            common = gitfile
        else:
            return
        ex = common / "info" / "exclude"
        ex.parent.mkdir(parents=True, exist_ok=True)
        cur = ex.read_text(encoding="utf-8") if ex.is_file() else ""
        add = [p for p in patterns if p not in cur.splitlines()]
        if add:
            ex.write_text(cur.rstrip("\n") + ("\n" if cur else "") + "\n".join(add) + "\n", encoding="utf-8")
    except (OSError, IndexError):
        return

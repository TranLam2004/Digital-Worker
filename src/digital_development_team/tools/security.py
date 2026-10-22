"""security.scan — bandit (SAST) · pip-audit (dependency CVE) · detect-secrets (secret) qua sandbox profile verifier.

Hàm `sast_scan` dùng chung cho tool (Security gọi) và slot `sast` của MACHINE_GATE (harness gọi) → một con số.
Quy ước: scanner đọc worktree, ghi stdout; parser thuần ở tools/quality; output đầy đủ → artifact; không in secret.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import CancelToken, ToolContext
from digital_development_team.core.errors import IsolationError, SandboxError
from digital_development_team.core.models import ExecResult, ToolResult
from digital_development_team.core.protocols import Sandbox
from digital_development_team.isolation.git import Git
from digital_development_team.sandbox.profiles import profile_for
from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.base import Handler
from digital_development_team.tools.exec import read_output
from digital_development_team.tools.quality import parse_bandit, parse_detect_secrets, parse_pip_audit
from digital_development_team.tools.quality.langs_sec import (
    parse_dotnet_vulnerable,
    parse_govulncheck,
    parse_npm_audit,
    parse_sarif,
)

BINARIES = {"sast": "bandit", "secrets": "detect-secrets", "deps": "pip-audit"}
DEFAULT_TARGETS = ("sast", "secrets")
DEP_FILE_HINTS = ("requirements", "pyproject.toml", "poetry.lock", "pipfile", "setup.py", "setup.cfg")
_SKIP_DIRS = (".git/", ".codegraph/", "__pycache__/", ".venv/", "node_modules/")


def tool_available(target: str) -> bool:
    return shutil.which(BINARIES[target]) is not None


def _run(
    sandbox: Sandbox, cfg: SandboxCfg, wt: Path, cmd: str, cancel: CancelToken, timeout_s: float
) -> tuple[ExecResult, str, str]:
    prof = profile_for(cfg, "verifier", wt)  # chỉ đọc worktree; writable chỉ /tmp
    res = sandbox.run(cmd, cwd=wt, profile=prof, timeout_s=timeout_s, cancel=cancel)
    out, err = read_output(res)
    return res, out, err


def _artifact(artifacts: ArtifactStore, issue: str, cmd: str, out: str, err: str, label: str) -> str:
    return artifacts.put(issue, f"$ {cmd}\n{out}\n--- stderr ---\n{err}", ext="log", label=label)


def _existing(wt: Path, files: Sequence[str]) -> list[str]:
    return [
        f
        for f in files
        if not any(f.replace("\\", "/").startswith(d) for d in _SKIP_DIRS) and (wt / f).is_file()
    ]


def _bandit_scan(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    artifacts: ArtifactStore,
    issue: str,
    wt: Path,
    files: Sequence[str],
    cancel: CancelToken,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """bandit -f json trên file .py đã đổi → {status pass|fail|skipped, summary, output_ref?, high, medium, low, findings[]}.
    fail ⇔ high > 0. Thiếu bandit / không file .py → skipped (không chặn)."""
    py = [f for f in _existing(wt, files) if f.endswith(".py")]
    if not tool_available("sast"):
        return {
            "status": "skipped",
            "summary": "bandit không có trong PATH (pip install -e '.[quality]')",
            "high": 0,
            "medium": 0,
            "low": 0,
        }
    if not py:
        return {
            "status": "skipped",
            "summary": "không có file .py trong diff",
            "high": 0,
            "medium": 0,
            "low": 0,
        }
    cmd = "bandit -q -f json " + " ".join(shlex.quote(f) for f in py)
    try:
        res, out, err = _run(sandbox, cfg, wt, cmd, cancel, timeout_s)
    except SandboxError as e:
        return {
            "status": "skipped",
            "summary": f"sandbox: {e.message}"[:200],
            "high": 0,
            "medium": 0,
            "low": 0,
        }
    ref = _artifact(artifacts, issue, cmd, out, err, "sast-bandit")
    if res.timed_out:
        return {
            "status": "fail",
            "summary": "bandit timeout",
            "output_ref": ref,
            "high": 0,
            "medium": 0,
            "low": 0,
        }
    parsed = parse_bandit(out)
    if parsed.get("errors") and parsed["total"] == 0 and res.exit_code not in (0, 1):
        return {
            "status": "fail",
            "summary": f"bandit exit {res.exit_code}: {parsed['errors'][0]}"[:200],
            "output_ref": ref,
            **parsed,
        }
    status = "fail" if parsed["high"] > 0 else "pass"
    summary = f"bandit: {parsed['high']} high · {parsed['medium']} medium · {parsed['low']} low trên {len(py)} file .py"
    return {"status": status, "summary": summary, "output_ref": ref, **parsed}


def secrets_scan(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    artifacts: ArtifactStore,
    issue: str,
    wt: Path,
    files: Sequence[str],
    cancel: CancelToken,
    *,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """detect-secrets scan <files> → {status pass|fail|skipped, summary, output_ref?, count, findings[]}. fail ⇔ count > 0."""
    targets = _existing(wt, files)
    if not tool_available("secrets"):
        return {"status": "skipped", "summary": "detect-secrets không có trong PATH", "count": 0}
    if not targets:
        return {"status": "skipped", "summary": "không có file đổi để quét secret", "count": 0}
    cmd = "detect-secrets scan " + " ".join(shlex.quote(f) for f in targets)
    try:
        res, out, err = _run(sandbox, cfg, wt, cmd, cancel, timeout_s)
    except SandboxError as e:
        return {"status": "skipped", "summary": f"sandbox: {e.message}"[:200], "count": 0}
    ref = _artifact(
        artifacts, issue, cmd, out, err if err else "(hashed_secret chỉ trong artifact)", "secrets-detect"
    )
    if res.timed_out:
        return {"status": "fail", "summary": "detect-secrets timeout", "output_ref": ref, "count": 0}
    parsed = parse_detect_secrets(out)
    status = "fail" if parsed["count"] > 0 else "pass"
    return {
        "status": status,
        "summary": f"detect-secrets: {parsed['count']} nghi secret trên {len(targets)} file",
        "output_ref": ref,
        **parsed,
    }


def _pip_audit_scan(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    artifacts: ArtifactStore,
    issue: str,
    wt: Path,
    files: Sequence[str],
    cancel: CancelToken,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """pip-audit -r <requirements*.txt trong files> -f json → {status, summary, output_ref?, high_cves, findings[]}. Cần mạng."""
    reqs = [
        f
        for f in _existing(wt, files)
        if Path(f).name.lower().startswith("requirements") and f.endswith(".txt")
    ]
    if not tool_available("deps"):
        return {"status": "skipped", "summary": "pip-audit không có trong PATH", "high_cves": 0}
    if not reqs:
        touched = [f for f in files if any(h in f.lower() for h in DEP_FILE_HINTS)]
        note = (
            f"diff chạm {', '.join(touched)} nhưng pip-audit chỉ đọc requirements*.txt"
            if touched
            else "không có requirements*.txt trong diff"
        )
        return {"status": "skipped", "summary": note[:200], "high_cves": 0}
    total = 0
    findings: list[dict[str, Any]] = []
    refs: list[str] = []
    for req in reqs:
        cmd = f"pip-audit -r {shlex.quote(req)} -f json --progress-spinner off"
        try:
            res, out, err = _run(sandbox, cfg, wt, cmd, cancel, timeout_s)
        except SandboxError as e:
            return {"status": "skipped", "summary": f"sandbox: {e.message}"[:200], "high_cves": 0}
        refs.append(_artifact(artifacts, issue, cmd, out, err, "deps-pip-audit"))
        if res.timed_out:
            return {
                "status": "fail",
                "summary": f"pip-audit timeout ({req})",
                "output_ref": refs[-1],
                "high_cves": 0,
            }
        parsed = parse_pip_audit(out)
        if not out.strip() and res.exit_code not in (0, 1):
            return {
                "status": "skipped",
                "summary": f"pip-audit exit {res.exit_code} (mạng?): {err.strip()[-150:]}",
                "output_ref": refs[-1],
                "high_cves": 0,
            }
        total += int(parsed["high_cves"])
        findings += parsed["findings"]
    return {
        "status": "fail" if total else "pass",
        "summary": f"pip-audit: {total} vuln trên {len(reqs)} file requirements",
        "output_ref": refs[0],
        "high_cves": total,
        "findings": findings[:50],
    }


_SOURCE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".cs", ".go", ".rb", ".php")
_LOCKFILES = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.sum",
    "packages.lock.json",
    "pom.xml",
    "gradle.lockfile",
    "requirements.txt",
    "poetry.lock",
    "Pipfile.lock",
)


def _merge(parts: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Gộp kết quả nhiều scanner: skipped khi không cái nào chạy; fail khi có high hoặc một scanner fail."""
    ran = [r for r in parts if r.get("status") != "skipped"]
    if not ran:
        return {
            "status": "skipped",
            "summary": f"{label}: " + "; ".join(str(r.get("summary")) for r in parts)[:180],
            "high": 0,
            "high_cves": 0,
        }
    high = sum(int(r.get("high", r.get("high_cves", 0)) or 0) for r in ran)
    fail = high > 0 or any(r.get("status") == "fail" for r in ran)
    out: dict[str, Any] = {
        "status": "fail" if fail else "pass",
        "summary": " · ".join(str(r.get("summary")) for r in ran)[:300],
        "high": high,
        "high_cves": high,
        "findings": [f for r in ran for f in r.get("findings") or []][:50],
        "scanners": [str(r.get("tool") or r.get("summary", "")).split(":")[0] for r in ran],
    }
    for k in ("medium", "low"):
        if any(k in r for r in ran):
            out[k] = sum(int(r.get(k, 0) or 0) for r in ran)
    refs = [r["output_ref"] for r in ran if r.get("output_ref")]
    if refs:
        out["output_ref"] = refs[0]
    return out


def _scanner(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    artifacts: ArtifactStore,
    issue: str,
    wt: Path,
    cancel: CancelToken,
    *,
    binary: str,
    cmd: str,
    parse: Callable[[str], dict[str, Any]],
    label: str,
    ok_exit: tuple[int, ...] = (0, 1),
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    if shutil.which(binary) is None:
        return {"status": "skipped", "summary": f"{binary} không có trong PATH", "high": 0}
    try:
        res, out, err = _run(sandbox, cfg, wt, cmd, cancel, timeout_s)
    except SandboxError as e:
        return {"status": "skipped", "summary": f"{label}: sandbox {e.message}"[:200], "high": 0}
    ref = _artifact(artifacts, issue, cmd, out, err, label)
    if res.timed_out:
        return {"status": "fail", "summary": f"{label} timeout", "output_ref": ref, "high": 0}
    parsed = parse(out)
    if parsed.get("errors") or res.exit_code not in ok_exit:  # không có kết quả hợp lệ ≠ sạch
        tail = (err.strip() or out.strip())[-150:]
        return {
            "status": "skipped",
            "summary": f"{label} exit {res.exit_code} (mạng/restore?): {tail}",
            "output_ref": ref,
            "high": 0,
        }
    high = int(parsed.get("high", 0))
    return {
        "status": "fail" if high else "pass",
        "summary": f"{label}: {high} high, {len(parsed.get('findings') or [])} findings",
        "output_ref": ref,
        **parsed,
    }


def sast_scan(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    artifacts: ArtifactStore,
    issue: str,
    wt: Path,
    files: Sequence[str],
    cancel: CancelToken,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """SAST trên file đã đổi: bandit cho .py · semgrep (SARIF) cho JS/TS/Java/C#/Go… nếu có. fail ⇔ high > 0."""
    parts = [_bandit_scan(sandbox, cfg, artifacts, issue, wt, files, cancel, timeout_s=timeout_s)]
    other = [f for f in _existing(wt, files) if f.endswith(_SOURCE_EXT)]
    if other:
        cmd = "semgrep scan --sarif --quiet --metrics off --config auto " + " ".join(
            shlex.quote(f) for f in other
        )
        parts.append(
            _scanner(
                sandbox,
                cfg,
                artifacts,
                issue,
                wt,
                cancel,
                binary="semgrep",
                cmd=cmd,
                parse=parse_sarif,
                label="semgrep",
                timeout_s=timeout_s,
            )
        )
    if len(parts) == 1:
        return parts[0]
    merged = _merge(parts, "sast")
    merged.setdefault("medium", 0)
    merged.setdefault("low", 0)
    return merged


def deps_scan(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    artifacts: ArtifactStore,
    issue: str,
    wt: Path,
    files: Sequence[str],
    cancel: CancelToken,
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """CVE của phụ thuộc theo manifest bị đổi trong diff: pip-audit · npm audit · govulncheck · dotnet --vulnerable ·
    osv-scanner (mọi lockfile). Cần mạng; không chạy được → skipped kèm lý do (không chặn)."""
    names = {Path(f).name for f in files}

    def dirs(name: str) -> list[str]:
        return sorted({str(Path(f).parent).replace("\\", "/") for f in files if Path(f).name == name})

    parts: list[dict[str, Any]] = [
        _pip_audit_scan(sandbox, cfg, artifacts, issue, wt, files, cancel, timeout_s=timeout_s)
    ]
    kw: dict[str, Any] = {"timeout_s": timeout_s}
    for d in dirs("package-lock.json") or (dirs("package.json") if "package-lock.json" in names else []):
        pre = "" if d in (".", "") else f"cd {shlex.quote(d)} && "
        parts.append(
            _scanner(
                sandbox,
                cfg,
                artifacts,
                issue,
                wt,
                cancel,
                binary="npm",
                cmd=f"{pre}npm audit --json",
                parse=parse_npm_audit,
                label="npm-audit",
                **kw,
            )
        )
    for d in dirs("go.mod") + [x for x in dirs("go.sum") if x not in dirs("go.mod")]:
        pre = "" if d in (".", "") else f"cd {shlex.quote(d)} && "
        parts.append(
            _scanner(
                sandbox,
                cfg,
                artifacts,
                issue,
                wt,
                cancel,
                binary="govulncheck",
                cmd=f"{pre}govulncheck -json ./...",
                parse=parse_govulncheck,
                label="govulncheck",
                ok_exit=(0, 3),
                **kw,
            )
        )
    for proj in [f for f in files if f.endswith((".csproj", ".fsproj"))]:
        parts.append(
            _scanner(
                sandbox,
                cfg,
                artifacts,
                issue,
                wt,
                cancel,
                binary="dotnet",
                cmd=f"dotnet list {shlex.quote(proj)} package --vulnerable --include-transitive --format json",
                parse=parse_dotnet_vulnerable,
                label="dotnet-vulnerable",
                **kw,
            )
        )
    locks = [f for f in files if Path(f).name in _LOCKFILES and (wt / f).is_file()]
    if locks and shutil.which("osv-scanner"):
        cmd = "osv-scanner scan --format sarif " + " ".join(f"-L {shlex.quote(x)}" for x in locks)
        parts.append(
            _scanner(
                sandbox,
                cfg,
                artifacts,
                issue,
                wt,
                cancel,
                binary="osv-scanner",
                cmd=cmd,
                parse=parse_sarif,
                label="osv-scanner",
                **kw,
            )
        )
    if len(parts) == 1:
        return parts[0]
    return _merge(parts, "deps")


class SecurityTools:
    def __init__(
        self,
        sandbox: Sandbox,
        cfg: SandboxCfg,
        artifacts: ArtifactStore,
        *,
        git: Git | None = None,
        base_ref: Callable[[ToolContext], str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._cfg = cfg
        self._artifacts = artifacts
        self._git = git
        self._base_ref = base_ref or (lambda _ctx: "main")

    def handlers(self) -> dict[str, Handler]:
        return {"scan": self.scan}

    def changed_files(self, ctx: ToolContext, wt: Path) -> list[str]:
        if self._git is None:
            return []
        try:
            base = self._base_ref(ctx)
            return sorted(
                {p for _, p in self._git.status_porcelain(cwd=wt)}
                | set(self._git.changed_files(cwd=wt, base=base))
            )
        except IsolationError:
            return []

    def scan(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if not ctx.worktree:
            return ToolResult.fail("NO_WORKTREE", "stage này không có worktree")
        wt = Path(ctx.worktree)
        targets = [str(t) for t in (args.get("targets") or DEFAULT_TARGETS)]
        bad = [t for t in targets if t not in BINARIES]
        if bad:
            return ToolResult.fail("BAD_ARGS", f"targets lạ: {bad}", hint=f"chọn trong {sorted(BINARIES)}")
        paths = [str(p) for p in (args.get("paths") or [])]
        files = self._expand(wt, paths) if paths else self.changed_files(ctx, wt)
        out: dict[str, Any] = {"targets": targets, "files_scanned": len(files)}
        refs: list[str] = []
        parts: list[str] = []
        for t in targets:
            fn = {"sast": sast_scan, "secrets": secrets_scan, "deps": deps_scan}[t]
            r = fn(self._sandbox, self._cfg, self._artifacts, ctx.issue_id, wt, files, ctx.cancel)
            if r.get("output_ref"):
                refs.append(str(r.pop("output_ref")))
            out[t] = r
            parts.append(f"{t}={r['status']}")
        summary = "security.scan → " + " · ".join(parts) + (f" · {len(files)} file" if files else " · 0 file")
        return ToolResult(status="ok", summary=summary[:300], structured=out, artifact_refs=tuple(refs))

    @staticmethod
    def _expand(wt: Path, paths: Sequence[str]) -> list[str]:
        files: list[str] = []
        for p in paths:
            root = wt / p
            if root.is_dir():
                files += [str(x.relative_to(wt)).replace("\\", "/") for x in root.rglob("*") if x.is_file()]
            elif root.is_file():
                files.append(p.replace("\\", "/"))
        return _existing(wt, sorted(set(files)))

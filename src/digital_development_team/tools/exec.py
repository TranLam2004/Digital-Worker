"""exec.run — chạy lệnh qua Sandbox theo profile (dev · verifier/readonly · scan). exit≠0 = thông tin, không phải lỗi tool.

readonly: lệnh ghi bị policy/command.py chặn trước; lọt qua (vd script ghi file) thì snapshot mtime trước/sau
phát hiện → EROFS, và nếu worktree sạch trước khi chạy thì tự hoàn tác từ HEAD.
scan: cmd phải bắt đầu bằng tool trong cmd_allowlist của profile.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import ToolContext
from digital_development_team.core.errors import DDTError, SandboxError
from digital_development_team.core.models import ExecResult, ToolResult
from digital_development_team.core.protocols import Sandbox
from digital_development_team.isolation.git import Git
from digital_development_team.sandbox.profiles import PROFILE_FOR_VARIANT, profile_for
from digital_development_team.tools.base import Handler
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.paths import UnsafePath, resolve_inside

CACHE_DIRS = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    ".nox",
    "node_modules",
    ".gradle",
    ".nyc_output",
    "TestResults",
    "obj",
    "target",
    "coverage",
    ".next",
)
"""Thư mục cache công cụ tự tạo khi chạy test: không tính là ghi file."""


def _is_cache(rel: str) -> bool:
    parts = rel.split("/")
    return (
        parts[0] == ".git"
        or any(p in CACHE_DIRS for p in parts)
        or parts[-1] in (".coverage", "coverage.xml")
    )


def snapshot_mtimes(root: Path, git: Git | None = None) -> dict[str, float]:
    """mtime các file có thể là "ghi thật". Có git → chỉ file git thấy (theo dõi + mới chưa ignore): output build
    đã gitignore (node_modules, target/, bin/, obj/…) không bị tính là Verifier ghi file. Không git → bỏ CACHE_DIRS."""
    out: dict[str, float] = {}
    if git is not None:
        try:
            for rel in git.visible_files(cwd=root):
                if _is_cache(rel):
                    continue
                try:
                    out[rel] = (root / rel).stat().st_mtime_ns
                except OSError:
                    out[rel] = -1.0  # đã bị xoá
            return out
        except DDTError:
            out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if _is_cache(rel):
            continue
        if p.is_file():
            try:
                out[rel] = p.stat().st_mtime_ns
            except OSError:
                continue
    return out


def read_output(res: ExecResult) -> tuple[str, str]:
    def _r(p: str) -> str:
        path = Path(p)
        return path.read_text(encoding=ENCODING, errors="replace") if path.is_file() else ""

    return _r(res.stdout_path), _r(res.stderr_path)


class ExecTools:
    def __init__(self, sandbox: Sandbox, cfg: SandboxCfg, *, git: Git | None = None) -> None:
        self._sandbox = sandbox
        self._cfg = cfg
        self._git = git  # hoàn tác khi role chỉ đọc lỡ ghi file

    def _clean(self, wt: Path) -> bool:
        if self._git is None:
            return False
        try:  # cache test (__pycache__, .pytest_cache) chưa gitignore không tính là bẩn
            return all(_is_cache(path) for _, path in self._git.status_porcelain(cwd=wt))
        except DDTError:
            return False

    def _revert(self, wt: Path, before: Mapping[str, float], changed: list[str]) -> list[str]:
        """Worktree sạch trước khi chạy: file đã có → lấy lại từ HEAD, file mới → xoá. Trả danh sách đã hoàn tác."""
        done: list[str] = []
        if self._git is None:
            return done
        for rel in changed:
            if rel not in before:
                (wt / rel).unlink(missing_ok=True)
                done.append(rel)
            elif self._git.run(["checkout", "HEAD", "--", rel], cwd=wt).ok:
                done.append(rel)
        return done

    def handlers(self) -> dict[str, Handler]:
        return {"run": self.run}

    def run(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if not ctx.worktree:
            return ToolResult.fail("NO_WORKTREE", "stage này không có worktree")
        wt = Path(ctx.worktree)
        cmd = str(args.get("cmd", "")).strip()
        if not cmd:
            return ToolResult.fail("BAD_ARGS", "cmd rỗng")
        try:
            cwd = resolve_inside(wt, str(args.get("cwd") or ".")) if args.get("cwd") else wt
        except UnsafePath as e:
            return ToolResult.fail("BAD_ARGS", f"cwd không hợp lệ: {e}")
        pname = PROFILE_FOR_VARIANT.get(profile, "dev")
        try:
            prof = profile_for(self._cfg, pname, wt)
        except SandboxError as e:
            return ToolResult.fail("SANDBOX", e.message, hint=e.hint)
        if prof.cmd_allowlist is not None:
            first = (shlex.split(cmd, posix=True)[:1] or [""])[0]
            if first not in prof.cmd_allowlist:
                return ToolResult.denied(
                    "scan_allowlist",
                    f"profile scan chỉ cho phép: {', '.join(prof.cmd_allowlist)}",
                    hint="Dùng đúng scanner trong allowlist",
                    cmd=first,
                )
        timeout = float(args.get("timeout") or self._cfg.timeout_sec)
        before = snapshot_mtimes(wt, self._git) if pname == "verifier" else None
        clean_before = before is not None and self._clean(wt)
        try:
            res = self._sandbox.run(
                cmd, cwd=cwd, profile=prof, timeout_s=timeout, stdin=args.get("stdin"), cancel=ctx.cancel
            )
        except SandboxError as e:
            return ToolResult.fail("SANDBOX", e.message, hint=e.hint)
        stdout, stderr = read_output(res)
        structured: dict[str, Any] = {
            "exit_code": res.exit_code,
            "stdout_lines": res.stdout_lines,
            "stderr_lines": res.stderr_lines,
            "duration_ms": res.duration_ms,
            "stdout": stdout,
            "stderr": stderr,
            "profile": pname,
        }
        # tail chỉ có ích khi output dài (stdout có thể bị artifactize); ngắn thì là bản sao thừa — token mất trắng
        if res.stdout_lines > len(res.stdout_tail):
            structured["stdout_tail"] = list(res.stdout_tail)
        if res.stderr_lines > len(res.stderr_tail):
            structured["stderr_tail"] = list(res.stderr_tail)
        if res.timed_out:
            return ToolResult.fail(
                "TIMEOUT",
                f"exec '{cmd[:60]}' quá {timeout:g}s",
                hint="Lệnh chạy quá lâu. Chia nhỏ hoặc chạy subset test",
                **structured,
            )
        if res.cancelled:
            return ToolResult.fail("CANCELLED", "lệnh bị hủy", **structured)
        if before is not None:
            after = snapshot_mtimes(wt, self._git)
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            if changed:
                structured["fs_writes"] = changed[:20]
                reverted = self._revert(wt, before, changed) if clean_before else []
                structured["reverted"] = reverted[:20]
                undo = (
                    f", đã hoàn tác {len(reverted)}/{len(changed)}"
                    if clean_before
                    else ", không tự hoàn tác được vì worktree đã có thay đổi trước khi chạy"
                )
                return ToolResult.fail(
                    "EROFS",
                    f"profile readonly nhưng lệnh đã ghi {len(changed)} file{undo}",
                    hint="Bạn readonly. Chỉ chạy, không sửa.",
                    **structured,
                )
        return ToolResult.ok(
            f"exec '{cmd[:60]}' → exit {res.exit_code} in {res.duration_ms / 1000:.1f}s ({res.stdout_lines} lines)",
            **structured,
        )

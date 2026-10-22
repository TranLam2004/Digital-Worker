"""test.run / test.coverage / test.parse — wrapper trên sandbox chuyên cho test.

run:      parse structured, summary_line nguyên văn = bằng chứng. Verifier: cmd = spec.test_command, không filter,
          profile readonly. Dev: tự do (trong sandbox); `affected: true` → pytest --testmon (vòng trong, TESTMON_DATADIR ở TEMP).
coverage: pytest-cov + diff-cover so với base_commit (tools/coverage.py) — Verifier/runner, chỉ đọc worktree.
Full output LUÔN lưu artifact (kể cả nhỏ) để Verifier/người xem stack trace.
"""

from __future__ import annotations

import importlib.util
import shlex
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import ToolContext
from digital_development_team.core.errors import SandboxError
from digital_development_team.core.models import ToolResult
from digital_development_team.core.protocols import Sandbox
from digital_development_team.isolation.git import Git
from digital_development_team.sandbox.profiles import profile_for
from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.base import Handler
from digital_development_team.tools.coverage import coverage_run
from digital_development_team.tools.exec import read_output
from digital_development_team.tools.parsers import JUNIT_REPORT_DIRS, detect_runner, parse_output
from digital_development_team.tools.parsers.reports import read_junit_reports
from digital_development_team.util.hashing import sha256_text

SpecReader = Callable[[str], Mapping[str, Any] | None]
_FORBIDDEN = ("|| true", "; exit 0", "--no-verify", "PYTEST_ADDOPTS")


class TestTools:
    def __init__(
        self,
        sandbox: Sandbox,
        cfg: SandboxCfg,
        artifacts: ArtifactStore,
        *,
        spec_reader: SpecReader,
        git: Git | None = None,
        base_ref: Callable[[ToolContext], str] | None = None,
        baseline_path: Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._cfg = cfg
        self._artifacts = artifacts
        self._spec = spec_reader
        self._git = git
        self._base_ref = base_ref or (lambda _ctx: "main")
        self._baseline = baseline_path

    def handlers(self) -> dict[str, Handler]:
        return {"run": self.run, "parse": self.parse, "coverage": self.coverage}

    def coverage(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if not ctx.worktree:
            return ToolResult.fail("NO_WORKTREE", "stage này không có worktree")
        wt = Path(ctx.worktree)
        spec = self._spec(ctx.issue_id) or {}
        cmd = str(spec.get("test_command") or "").strip()
        if ctx.actor.role == "runner" and args.get("cmd"):
            cmd = str(args["cmd"]).strip()
        if not cmd:
            return ToolResult.fail("BAD_ARGS", "spec.test_command trống", hint="chờ spec có test_command")
        r = coverage_run(
            self._sandbox,
            self._cfg,
            self._artifacts,
            ctx.issue_id,
            wt,
            cmd,
            self._base_ref(ctx),
            ctx.cancel,
            baseline_path=self._baseline,
            timeout_s=300.0,
        )
        if r["status"] == "skipped":
            return ToolResult.fail(
                "NOT_AVAILABLE", str(r["summary"]), hint="pip install -e '.[quality]' hoặc chỉ dùng test.run"
            )
        ref = r.pop("output_ref", None)
        return ToolResult(
            status="ok",
            summary=f"test.coverage → {r['summary']}"[:300],
            structured=r,
            artifact_refs=(str(ref),) if ref else (),
        )

    def run(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if not ctx.worktree:
            return ToolResult.fail("NO_WORKTREE", "stage này không có worktree")
        wt = Path(ctx.worktree)
        spec = self._spec(ctx.issue_id) or {}
        default_cmd = str(spec.get("test_command") or "")
        cmd = str(args.get("cmd") or default_cmd).strip()
        if not cmd:
            return ToolResult.fail(
                "BAD_ARGS",
                "không có cmd và spec.test_command trống",
                hint="truyền cmd (Dev) hoặc chờ spec có test_command",
            )
        role = ctx.actor.role
        if role == "verifier":
            allowed = {
                str(spec.get(k)).strip() for k in ("test_command", "regression_command") if spec.get(k)
            }
            if cmd not in allowed:
                return ToolResult.denied(
                    "verifier_cmd",
                    "Verifier chỉ chạy đúng spec.test_command / regression_command",
                    hint=f"dùng: {sorted(allowed)}",
                )
            if args.get("filter") or args.get("affected"):
                return ToolResult.denied(
                    "verifier_filter", "Verifier không được filter/affected — phải chạy đúng như spec"
                )
        elif role != "runner" and args.get("cmd"):
            # test.run không phải exec thứ hai: lệnh tự truyền phải là test runner, hoặc đúng lệnh trong spec
            spec_cmds = {
                str(spec.get(k)).strip() for k in ("test_command", "regression_command") if spec.get(k)
            }
            if detect_runner(cmd) == "generic" and cmd not in spec_cmds:
                return ToolResult.denied(
                    "test_runner_only",
                    "test.run chỉ chạy test runner (pytest, node --test, jest, vitest, go test, cargo test, mvn, gradle, dotnet test, npm/yarn/pnpm test)",
                    hint="Lệnh khác dùng exec.run; lệnh test riêng của repo thì để Analyst ghi vào spec.test_command",
                )
        if any(f in cmd for f in _FORBIDDEN):
            return ToolResult.denied(
                "fake_pass",
                "cmd chứa pattern giả pass",
                hint="Bỏ `|| true`, `; exit 0`, --no-verify, PYTEST_ADDOPTS",
            )
        runner = detect_runner(cmd)
        if args.get("filter"):
            flt = shlex.quote(str(args["filter"]))
            cmd = {
                "pytest": f"{cmd} -k {flt}",
                "jest": f"{cmd} -t {flt}",
                "vitest": f"{cmd} -t {flt}",
                "node": f"{cmd} --test-name-pattern={flt}",
                "go": f"{cmd} -run {flt}",
                "maven": f"{cmd} -Dtest={flt}",
                "dotnet": f"{cmd} --filter {flt}",
            }.get(runner, cmd)
        affected_mode: str | None = None
        if args.get("affected") and role != "verifier":
            if runner == "pytest" and importlib.util.find_spec("testmon") is not None:
                datadir = (  # khóa theo worktree: cùng issue id ở repo khác không dùng chung dữ liệu testmon
                    Path(tempfile.gettempdir())
                    / "ddt-testmon"
                    / sha256_text(str(wt.resolve())).rsplit(":", 1)[-1][:16]
                )
                datadir.mkdir(parents=True, exist_ok=True)
                cmd = f"TESTMON_DATAFILE={shlex.quote(str(datadir / '.testmondata').replace(chr(92), '/'))} {cmd} --testmon"
                affected_mode = "testmon"
            else:
                affected_mode = "fallback_full"  # thiếu plugin / không phải pytest → chạy full, không im lặng
        pname = "verifier" if role == "verifier" else "dev"
        started = time.time()
        try:
            prof = profile_for(self._cfg, pname, wt)
            res = self._sandbox.run(
                cmd, cwd=wt, profile=prof, timeout_s=float(args.get("timeout") or 240), cancel=ctx.cancel
            )
        except SandboxError as e:
            return ToolResult.fail("SANDBOX", e.message, hint=e.hint)
        stdout, stderr = read_output(res)
        full = f"$ {cmd}\n{stdout}\n--- stderr ---\n{stderr}"
        ref = self._artifacts.put(ctx.issue_id, full, ext="log", label=f"test-{runner}")
        if res.timed_out:
            return ToolResult(
                status="error",
                summary=f"test.run timeout {args.get('timeout') or 240}s",
                structured={"cmd": cmd, "runner": runner},
                artifact_refs=(ref,),
                preview=ArtifactStore.preview(stdout),
                error=__import__("digital_development_team.core.models", fromlist=["ToolError"]).ToolError(
                    code="TIMEOUT", message="test quá lâu", hint="Chạy subset hoặc tăng timeout"
                ),
            )
        parsed = parse_output(runner, stdout, stderr, res.exit_code)
        if runner in JUNIT_REPORT_DIRS and not (parsed.get("passed") or parsed.get("failed")):
            rep = read_junit_reports(wt, JUNIT_REPORT_DIRS[runner], since=started - 1)
            if rep is not None:  # mvn -q không in số liệu: lấy từ báo cáo JUnit XML vừa ghi
                parsed.update(rep)
        parsed["cmd"] = cmd
        parsed["duration_ms"] = res.duration_ms
        if affected_mode:
            parsed["affected_mode"] = affected_mode
            if affected_mode == "testmon" and parsed.get("status") == "no_tests":
                # testmon không chọn test nào = không test nào bị ảnh hưởng → không phải lỗi
                parsed["status"] = "pass"
                parsed["summary_line"] = "testmon: 0 test bị ảnh hưởng bởi thay đổi (chạy full trước khi nộp)"
        failed = parsed.get("failed_tests") or []
        names = (
            ", ".join(str(f["name"]).rsplit("::", 1)[-1] for f in failed[:3])
            if isinstance(failed, list)
            else ""
        )
        if parsed["status"] == "pass":
            summary = f"test.run → {parsed['summary_line'] or 'pass'}"
        elif parsed["status"] == "no_tests":
            summary = "test.run → không có test nào chạy"
        elif parsed["status"] == "error":
            summary = f"test.run → lỗi chạy test (exit {res.exit_code}) — xem artifact"
        else:
            summary = f"test.run → {parsed.get('passed', 0)} passed, {parsed.get('failed', 0)} failed" + (
                f" ({names})" if names else ""
            )
        return ToolResult(
            status="ok",
            summary=summary[:300],
            structured=parsed,
            artifact_refs=(ref,),
            preview=ArtifactStore.preview(stdout, 5, 15),
        )

    def parse(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        ref = str(args.get("artifact", ""))
        text = self._artifacts.read(ref)
        if text is None:
            return ToolResult.fail("NOT_FOUND", f"artifact {ref} không có")
        runner = str(
            args.get("runner")
            or (
                "junit"
                if text.lstrip().startswith("<")
                else detect_runner(text.splitlines()[0] if text else "")
            )
        )
        parsed = parse_output(runner, text, "", 0 if " passed" in text and " failed" not in text else 1)
        return ToolResult.ok(f"test.parse → {parsed.get('summary_line') or parsed['status']}", **parsed)

"""Onboarding một repo đích (`ddt onboard`): nhận diện stack → kiểm toolchain → probe lệnh trong worktree tạm →
ghi bảng Build & test vào <home>/memory/REPO.md (machine gate + Analyst đọc) và SETUP.yaml (bước cài phụ thuộc).

Không sửa repo của người dùng: probe chạy trong worktree tách rời của HEAD, xoá sau khi xong.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import DDTError
from digital_development_team.core.protocols import Sandbox
from digital_development_team.isolation.git import Git
from digital_development_team.onboarding.detect import Stack, detect_stacks, missing_tools
from digital_development_team.sandbox.profiles import profile_for
from digital_development_team.tools.exec import read_output
from digital_development_team.tools.parsers import JUNIT_REPORT_DIRS, detect_runner, parse_output
from digital_development_team.tools.parsers.reports import read_junit_reports
from digital_development_team.util import yamlio
from digital_development_team.util.fsio import ENCODING, write_text_atomic

__all__ = ["OnboardReport", "Stack", "onboard"]

KINDS = ("build", "lint", "typecheck", "test")
_TABLE_START = "## Build & test"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class Probe:
    kind: str
    cmd: str
    ok: bool
    note: str


@dataclass
class OnboardReport:
    stacks: list[Stack]
    missing: dict[str, str]
    probes: list[Probe] = field(default_factory=list)
    chosen: dict[str, str] = field(default_factory=dict)
    restore_log: list[Probe] = field(default_factory=list)
    repo_md: Path | None = None
    setup: Path | None = None


def onboard(
    repo: Path,
    *,
    git: Git,
    sandbox: Sandbox,
    sandbox_cfg: SandboxCfg,
    memory_dir: Path,
    scratch: Path,
    install: bool = False,
    dry_run: bool = False,
    timeout_s: float = 600.0,
    log: Callable[[str], None] = lambda _m: None,
    scope_dir: str | None = None,
) -> OnboardReport:
    files = git.visible_files(cwd=repo)
    if scope_dir and scope_dir != ".":
        prefix = scope_dir.replace("\\", "/").rstrip("/") + "/"
        files = [f for f in files if f.startswith(prefix)]

    def read(rel: str) -> str | None:
        p = repo / rel
        return p.read_text(encoding=ENCODING, errors="replace") if p.is_file() else None

    stacks = detect_stacks(files, read)
    rep = OnboardReport(stacks=stacks, missing=missing_tools(stacks))
    wt = scratch / f"onboard-{uuid.uuid4().hex[:8]}"
    git.run(["worktree", "add", "-q", "--detach", str(wt), "HEAD"]).check("worktree add --detach")
    try:
        prof = profile_for(sandbox_cfg, "dev", wt)
        if install:
            for st in stacks:
                for cmd in st.restore:
                    log(f"cài phụ thuộc: {cmd}")
                    rep.restore_log.append(_run(sandbox, prof, wt, cmd, timeout_s, kind="restore"))
        for st in stacks:
            if any(t in rep.missing for t in st.tools):
                continue
            for kind in KINDS:
                for cmd in st.commands.get(kind, []):
                    log(f"thử {kind}: {cmd}")
                    pr = _run(sandbox, prof, wt, cmd, timeout_s, kind=kind)
                    rep.probes.append(pr)
                    if pr.ok:
                        key = kind if kind not in rep.chosen else f"{kind}:{st.language}"
                        rep.chosen.setdefault(key, cmd)
                        break
    finally:
        with contextlib.suppress(DDTError):
            git.worktree_remove(wt, force=True)
    if not dry_run:
        memory_dir.mkdir(parents=True, exist_ok=True)
        rep.repo_md = memory_dir / "REPO.md"
        write_text_atomic(rep.repo_md, merge_repo_md(read_existing(rep.repo_md), rep), fsync=False)
        rep.setup = memory_dir / "SETUP.yaml"
        write_text_atomic(rep.setup, yamlio.dumps(setup_doc(rep)), fsync=False)
    return rep


def _run(sandbox: Sandbox, prof: Any, wt: Path, cmd: str, timeout_s: float, *, kind: str) -> Probe:
    t0 = time.monotonic()
    started = time.time()
    try:
        res = sandbox.run(cmd, cwd=wt, profile=prof, timeout_s=timeout_s, cancel=CancelToken())
    except DDTError as e:
        return Probe(kind, cmd, False, e.message[:200])
    secs = time.monotonic() - t0
    if res.timed_out:
        return Probe(kind, cmd, False, f"timeout after {timeout_s:.0f}s")
    stdout, stderr = read_output(res)
    stdout, stderr = _ANSI.sub("", stdout), _ANSI.sub("", stderr)
    if kind != "test":
        tail = next((ln for ln in reversed((stderr or stdout).splitlines()) if ln.strip()), "")
        return Probe(
            kind,
            cmd,
            res.exit_code == 0,
            f"exit {res.exit_code} in {secs:.0f}s" + (f": {tail[:120]}" if res.exit_code else ""),
        )
    runner = detect_runner(cmd)
    parsed = parse_output(runner, stdout, stderr, res.exit_code)
    if runner in JUNIT_REPORT_DIRS and not (parsed.get("passed") or parsed.get("failed")):
        rep = read_junit_reports(wt, JUNIT_REPORT_DIRS[runner], since=started - 1)
        if rep is not None:  # mvn -q: số liệu nằm trong báo cáo JUnit XML
            parsed.update(rep)
    status = str(parsed.get("status"))
    if status in ("error", "no_tests") or (
        status == "fail" and not parsed.get("failed") and not parsed.get("passed")
    ):
        tail = next((ln for ln in reversed((stderr or stdout).splitlines()) if ln.strip()), "")
        return Probe(kind, cmd, False, f"{status}: {tail[:150]}")
    counts = parsed.get("passed") or parsed.get("failed")
    note = (
        f"{parsed.get('passed', 0)} passed, {parsed.get('failed', 0)} failed in {secs:.0f}s"
        if counts
        else f"{status} in {secs:.0f}s ({parsed.get('summary_line')})"
    )
    if status == "fail":
        note += " (already failing on HEAD)"
    return Probe(kind, cmd, True, note)


# ───── REPO.md / SETUP.yaml ─────
def read_existing(path: Path) -> str | None:
    return path.read_text(encoding=ENCODING) if path.is_file() else None


def _table(rep: OnboardReport) -> str:
    rows = ["| Step | Command |", "|---|---|"]
    for kind in KINDS:
        cmd = rep.chosen.get(kind)
        rows.append(f"| {kind.capitalize()} | `{cmd}` |" if cmd else f"| {kind.capitalize()} | — |")
    for key, cmd in rep.chosen.items():
        if ":" in key:
            k, lang = key.split(":", 1)
            rows.append(f"| {k.capitalize()} ({lang}) | `{cmd}` |")
    return "\n".join(rows)


def merge_repo_md(existing: str | None, rep: OnboardReport) -> str:
    """Thay đúng mục `## Build & test` (giữ phần người viết); chưa có REPO.md → tạo khung tối thiểu."""
    langs = ", ".join(sorted({s.language for s in rep.stacks})) or "unknown"
    section = f"{_TABLE_START}\n<!-- ddt onboard: commands that ran on HEAD -->\n{_table(rep)}\n"
    if existing and _TABLE_START in existing:
        return re.sub(
            r"## Build & test\n.*?(?=\n## |\Z)", section.rstrip("\n"), existing, count=1, flags=re.S
        ) + ("" if existing.endswith("\n") else "\n")
    head = existing.rstrip() + "\n\n" if existing else f"# REPO.md\n\n## Overview\nLanguages: {langs}.\n\n"
    return head + section


def setup_doc(rep: OnboardReport) -> dict[str, Any]:
    return {
        "languages": sorted({s.language for s in rep.stacks}),
        "missing_tools": rep.missing,
        "restore": [c for s in rep.stacks for c in s.restore],
        "commands": rep.chosen,
    }

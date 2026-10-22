"""Coverage trên dòng vừa sửa: pytest-cov (coverage.xml) + diff-cover (JSON) so với base_commit — dùng chung cho
`test.coverage` (Verifier gọi) và slot `coverage` của MACHINE_GATE (harness gọi).

Quy ước: mọi file tạm (COVERAGE_FILE, coverage.xml, diff.json) nằm ở TEMP, worktree không đổi (profile verifier).
Baseline `<home>/memory/coverage_baseline.json` {total_pct, commit, at}: runner tạo lần đầu / cập nhật sau MERGE.
Slot coverage KHÔNG bao giờ fail — chỉ pass|skipped; số đo đi vào test_refs cho TEST-004/005 và confidence.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import SandboxError
from digital_development_team.core.protocols import Sandbox
from digital_development_team.sandbox.profiles import profile_for
from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.exec import read_output
from digital_development_team.tools.parsers import detect_runner, parse_output
from digital_development_team.tools.quality import parse_coverage_xml, parse_diff_cover
from digital_development_team.util.fsio import ENCODING, write_text_atomic

BASELINE_FILE = "coverage_baseline.json"


def coverage_available() -> str | None:
    """None = đủ tool; chuỗi = thiếu gì."""
    missing = []
    if importlib.util.find_spec("pytest_cov") is None:
        missing.append("pytest-cov")
    if shutil.which("diff-cover") is None:
        missing.append("diff-cover")
    return None if not missing else "thiếu " + ", ".join(missing) + " (pip install -e '.[quality]')"


def read_baseline(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) and "total_pct" in d else None


def write_baseline(path: Path, total_pct: float, commit: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"total_pct": round(float(total_pct), 1), "commit": commit, "at": datetime.now(UTC).isoformat()}
    write_text_atomic(path, json.dumps(body, ensure_ascii=False, indent=1), fsync=False)


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def coverage_run(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    artifacts: ArtifactStore,
    issue: str,
    wt: Path,
    cmd: str,
    base: str,
    cancel: CancelToken,
    *,
    baseline_path: Path | None,
    timeout_s: float = 240.0,
    create_baseline: bool = False,
    commit: str | None = None,
) -> dict[str, Any]:
    """→ {status pass|skipped, summary, output_ref?, total_pct, baseline_pct, delta, diff_pct, new_lines,
    uncovered_count, uncovered_new_lines[], files[], test{passed, failed, status}}."""
    missing = coverage_available()
    if missing:
        return {"status": "skipped", "summary": missing[:200]}
    if detect_runner(cmd) != "pytest":
        return {"status": "skipped", "summary": "coverage chỉ hỗ trợ pytest (P2: jest/go)"}
    tmp = Path(tempfile.mkdtemp(prefix=f"ddt-cov-{issue}-"))
    xml = tmp / "coverage.xml"
    report = tmp / "diff-cover.json"
    prof = profile_for(cfg, "verifier", wt)
    cov_cmd = (
        f"COVERAGE_FILE={shlex.quote(_posix(tmp / '.coverage'))} {cmd} "
        f"--cov --cov-report=xml:{shlex.quote(_posix(xml))} -p no:cacheprovider"
    )
    try:
        res = sandbox.run(cov_cmd, cwd=wt, profile=prof, timeout_s=timeout_s, cancel=cancel)
    except SandboxError as e:
        return {"status": "skipped", "summary": f"sandbox: {e.message}"[:200]}
    out, err = read_output(res)
    ref = artifacts.put(
        issue, f"$ {cov_cmd}\n{out}\n--- stderr ---\n{err}", ext="log", label="coverage-pytest"
    )
    if res.timed_out:
        return {"status": "skipped", "summary": f"coverage timeout {int(timeout_s)}s", "output_ref": ref}
    test = parse_output("pytest", out, err, res.exit_code)
    if not xml.is_file():
        return {
            "status": "skipped",
            "summary": f"không có coverage.xml (exit {res.exit_code}; pytest-cov chưa nạp?)"[:200],
            "output_ref": ref,
            "test": {k: test.get(k) for k in ("passed", "failed", "status")},
        }
    cov = parse_coverage_xml(xml.read_text(encoding=ENCODING, errors="replace"))
    dc_cmd = f"diff-cover {shlex.quote(_posix(xml))} --compare-branch={shlex.quote(base)} --format json:{shlex.quote(_posix(report))}"
    try:
        res2 = sandbox.run(dc_cmd, cwd=wt, profile=prof, timeout_s=min(timeout_s, 120.0), cancel=cancel)
    except SandboxError as e:
        return {"status": "skipped", "summary": f"sandbox: {e.message}"[:200], "output_ref": ref}
    out2, err2 = read_output(res2)
    artifacts.put(issue, f"$ {dc_cmd}\n{out2}\n--- stderr ---\n{err2}", ext="log", label="coverage-diff")
    diff = (
        parse_diff_cover(report.read_text(encoding=ENCODING, errors="replace"))
        if report.is_file()
        else parse_diff_cover("")
    )
    total = cov.get("total_pct")
    baseline = read_baseline(baseline_path)
    baseline_pct = baseline.get("total_pct") if baseline else None
    delta = (
        round(float(total) - float(baseline_pct), 1)
        if (total is not None and baseline_pct is not None)
        else None
    )
    if baseline is None and create_baseline and baseline_path is not None and total is not None:
        write_baseline(baseline_path, total, commit)
    uncovered_count = int(diff.get("uncovered_count") or 0)
    summary = f"coverage {total}%"
    if delta is not None:
        summary += f" ({'+' if delta >= 0 else ''}{delta} vs baseline {baseline_pct}%)"
    if diff.get("diff_pct") is not None:
        summary += f" · dòng mới {diff['diff_pct']}% ({uncovered_count} chưa phủ)"
    else:
        summary += " · không có dòng mới trong diff"
    return {
        "status": "pass",
        "summary": summary[:200],
        "output_ref": ref,
        "total_pct": total,
        "baseline_pct": baseline_pct,
        "delta": delta,
        "diff_pct": diff.get("diff_pct"),
        "new_lines": diff.get("new_lines", 0),
        "uncovered_count": uncovered_count,
        "uncovered_new_lines": diff.get("uncovered_new_lines", []),
        "files": [f for f in cov.get("files", []) if not str(f["path"]).startswith("tests")][:20],
        "test": {k: test.get(k) for k in ("passed", "failed", "status")},
    }

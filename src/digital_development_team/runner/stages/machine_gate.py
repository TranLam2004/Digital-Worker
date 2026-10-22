"""MACHINE_GATE — 0 LLM, chạy TRƯỚC Verifier: build · lint · typecheck · unit_test (spec.test_command) ·
regression (spec.regression_command) · sast (bandit trên file .py đổi; fail ⇔ high > 0; skipped khi thiếu tool) ·
checklist regex trên diff + item `verify: test` trên test_refs · scope violation.

Lệnh build/lint/typecheck lấy từ REPO.md (dòng `- build_command: \\`…\\``), không có → skipped.
Kết quả → facts.machine_gate (transitions) + HarnessFields cho evidence.json (Verifier chỉ đọc, không điền).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from digital_development_team.checklist.runner import run_regex, run_test_refs
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import ConfigError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import StageResult
from digital_development_team.policy.command import check_command
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.blast_radius import as_scope_facts, compute_blast_radius
from digital_development_team.runner.state import IssueState
from digital_development_team.sandbox.profiles import profile_for
from digital_development_team.tools.codegraph import CodeGraphTools, cg_available, is_indexed
from digital_development_team.tools.coverage import BASELINE_FILE, coverage_run
from digital_development_team.tools.exec import read_output
from digital_development_team.tools.parsers import JUNIT_REPORT_DIRS, detect_runner, parse_output
from digital_development_team.tools.parsers.reports import read_junit_reports
from digital_development_team.tools.security import sast_scan
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.globs import GlobSet
from digital_development_team.util.paths import normcase

_CMD_RE = re.compile(r"^\s*-\s*(build|lint|typecheck)_command:\s*`?([^`\n]+)`?\s*$", re.M)
# bảng "Build & test" trong template REPO.md: | Build | `npm run build` | … |  (ô lệnh có thể là `—` = không có)
_TABLE_RE = re.compile(r"^\|\s*(Build|Lint|Typecheck)\s*\|\s*`([^`\n]+)`", re.M | re.I)
_NO_CMD = {"", "—", "-", "n/a", "none"}


def repo_commands(repo_md: str | None) -> dict[str, str]:
    r"""REPO.md → {build|lint|typecheck: cmd}. Nhận cả dòng `- lint_command: …` lẫn hàng bảng `| Lint | \`cmd\` |`;
    dòng list thắng bảng. `—` / trống = không khai báo."""
    if not repo_md:
        return {}
    out: dict[str, str] = {}
    for m in _TABLE_RE.finditer(repo_md):
        cmd = m.group(2).strip()
        if cmd.lower() not in _NO_CMD:
            out[m.group(1).lower()] = cmd
    for m in _CMD_RE.finditer(repo_md):
        cmd = m.group(2).strip()
        if cmd.lower() not in _NO_CMD:
            out[m.group(1)] = cmd
    return out


class MachineGateStage:
    stage = Stage.MACHINE_GATE
    uses_llm = False

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        svc = ctx.svc
        if svc.sandbox is None or svc.sandbox_cfg is None or svc.artifacts is None or svc.worktrees is None:
            raise ConfigError("MACHINE_GATE cần sandbox + repo", hint="chạy với --repo")
        if not state.worktree:
            raise ConfigError("MACHINE_GATE: chưa có worktree (IMPLEMENT chưa chạy?)")
        wt = Path(state.worktree)
        issue = state.issue_id
        spec = self._spec(ctx, issue)
        repo_md = self._repo_md(ctx, wt)
        cmds = repo_commands(repo_md)
        gates: dict[str, dict[str, Any]] = {}
        for name in ("build", "lint", "typecheck"):
            cmd = cmds.get(name)
            gates[name] = (
                self._run(ctx, wt, cmd, name)
                if cmd
                else {"status": "skipped", "summary": "REPO.md không khai báo"}
            )
        baseline = _baseline(state)
        test_cmd = str(spec.get("test_command") or "")
        gates["unit_test"] = (
            _minus_baseline(self._run(ctx, wt, test_cmd, "unit_test"), baseline)
            if test_cmd
            else {"status": "skipped", "summary": "spec không có test_command"}
        )
        reg_cmd = str(spec.get("regression_command") or "")
        regression = (
            _minus_baseline(self._run(ctx, wt, reg_cmd, "regression"), baseline)
            if reg_cmd and reg_cmd != test_cmd
            else {"status": "skipped", "summary": "không có regression_command riêng"}
        )

        # checklist regex trên diff + scope
        git = svc.worktrees.git
        base = state.base_commit or svc.worktrees.default_branch()
        diff = git.diff_worktree(cwd=wt, base=base)
        files = sorted(
            {p for _, p in git.status_porcelain(cwd=wt)} | set(git.changed_files(cwd=wt, base=base))
        )
        # sast trên file .py đã đổi — cùng hàm với security.scan → Security và harness thấy cùng số
        sast = sast_scan(
            svc.sandbox,
            svc.sandbox_cfg,
            svc.artifacts,
            issue,
            wt,
            files,
            ctx.cancel,
            timeout_s=float(ctx.config.budgets.tools.test),
        )
        gates["sast"] = sast
        test_refs: dict[str, Any] = {}
        if sast["status"] != "skipped":
            test_refs["sast_scan"] = {"high_findings": int(sast.get("high", 0))}
        # coverage trên dòng vừa sửa — không bao giờ fail; số đo → test_refs (TEST-004/005) + confidence
        safe_test_cmd = (
            bool(test_cmd) and check_command(test_cmd, ctx.config.policy.command_guard, worktree=str(wt)).ok
        )
        coverage = (
            coverage_run(
                svc.sandbox,
                svc.sandbox_cfg,
                svc.artifacts,
                issue,
                wt,
                test_cmd,
                base,
                ctx.cancel,
                baseline_path=svc.layout.memory_dir / BASELINE_FILE,
                timeout_s=float(ctx.config.budgets.tools.test),
                create_baseline=True,
                commit=state.base_commit,
            )
            if safe_test_cmd
            else {"status": "skipped", "summary": "spec không có test_command hoặc lệnh bị chặn"}
        )
        gates["coverage"] = coverage
        if coverage.get("delta") is not None:
            test_refs["coverage_delta"] = coverage["delta"]
        if coverage["status"] == "pass":
            test_refs["uncovered_new_lines"] = int(coverage.get("uncovered_count") or 0)
        tooling = {"sast": sast["status"] != "skipped", "coverage": coverage["status"] != "skipped"}
        # blast radius của patch: ast (symbol đổi) + CodeGraph impact (repo chính) → FMEA RPN; không có index → skipped
        repo_root = git.root
        cgt = CodeGraphTools(
            svc.sandbox,
            svc.sandbox_cfg,
            repo_root,
            git=git,
            thresholds=ctx.config.policy.codegraph_impact_thresholds,
        )
        br_depth = ctx.config.policy.blast_radius.depth
        use_cli = cg_available() and is_indexed(repo_root)

        def impact_fn(
            sym: str, _c: CodeGraphTools = cgt, _d: int = br_depth, _cli: bool = use_cli
        ) -> dict[str, Any] | None:
            # CLI (index repo chính) nếu có; không thì PyGraph trên chính worktree (thấy cả patch → không stale)
            return _c.impact_of(sym, ctx.cancel, depth=_d) if _cli else _c._impact_py(sym, depth=_d, root=wt)

        blast = compute_blast_radius(
            wt,
            diff,
            files,
            impact_fn,
            depth=ctx.config.policy.blast_radius.depth,
            coverage_diff_pct=(
                float(coverage["diff_pct"]) if isinstance(coverage.get("diff_pct"), int | float) else None
            ),
            touches_sensitive=bool(getattr(state, "touches_sensitive", False)),
            rpn_high=ctx.config.policy.blast_radius.rpn_high,
            rpn_medium=ctx.config.policy.blast_radius.rpn_medium,
        )
        allowed = [str(x) for x in spec.get("allowed_files", [])]
        gs = GlobSet(allowed) if allowed else None
        scope_violation = [
            f
            for f in files
            if gs is not None and not gs.match(f) and normcase(f) not in {normcase(a) for a in allowed}
        ]
        chk_facts: dict[str, Any] = {}
        chk_results: list[dict[str, Any]] = []
        if svc.checklists is not None and ctx.config.policy.checklists.enabled:
            scope = {
                "intake": self._slot(ctx, issue, "intake.yaml"),
                "spec": spec,
                "plan": self._slot(ctx, issue, "plan.yaml"),
                "files_touched": files,
                "state": {"tier": state.tier.value},
                "repo": {"tooling": tooling},
                "test_refs": test_refs,
            }
            items = svc.checklists.select("patch", scope)
            run = run_regex(items, diff)
            for r in run_test_refs(items, test_refs):
                run.results.append(r)
                if r.id in run.skipped:
                    run.skipped.remove(r.id)
            chk_facts = run.facts(svc.checklists.lite_security_ids())
            chk_results = [r.to_evidence() for r in run.results]
            svc.sink.emit(
                EventDraft(
                    issue_id=issue,
                    actor="runner",
                    event=EventType.CHECKLIST_RUN,
                    stage=self.stage,
                    data={
                        "items": len(items),
                        "regex": len(run.results),
                        "fails": chk_facts["failed_ids"],
                        "verdict": chk_facts["verdict"],
                    },
                )
            )

        any_fail = any(g["status"] == "fail" for g in (*gates.values(), regression)) or bool(scope_violation)
        gate_cmds = {**cmds, "unit_test": test_cmd, "regression": reg_cmd}
        mg_failures = [
            _failure(name, g, gate_cmds.get(name))
            for name, g in (*gates.items(), ("regression", regression))
            if g["status"] == "fail"
        ]
        mg_fact = {
            "any_fail": any_fail,
            **{k: v["status"] for k, v in gates.items()},
            "regression": regression["status"],
            "sast_high": int(sast.get("high", 0)),
            "coverage_total": coverage.get("total_pct"),
            "coverage_delta": coverage.get("delta"),
            "uncovered_new_lines": int(coverage.get("uncovered_count") or 0),
            "blast_rpn": blast.get("rpn"),
            "blast_risk": blast.get("risk"),
            "scope_violation": scope_violation,
            "files_touched": files,
            "summary": " · ".join(f"{k}={v['status']}" for k, v in gates.items())
            + f" · regression={regression['status']}",
        }
        # harness điền vào evidence.json
        if svc.harness_fields is not None:
            ref = svc.store.latest(issue, "evidence.json")
            svc.harness_fields.set(
                issue,
                "evidence.json",
                {
                    "version": (ref.version + 1) if ref else 1,
                    "machine_gate": {
                        **{
                            k: {
                                kk: vv
                                for kk, vv in v.items()
                                if kk in ("status", "output_ref", "summary", "high", "medium", "low")
                            }
                            for k, v in gates.items()
                        },
                        **(
                            {"coverage_delta": coverage["delta"]} if coverage.get("delta") is not None else {}
                        ),
                        **(
                            {"coverage_total": coverage["total_pct"]}
                            if coverage.get("total_pct") is not None
                            else {}
                        ),
                        **(
                            {"uncovered_new_lines": coverage["uncovered_new_lines"]}
                            if coverage.get("uncovered_new_lines")
                            else {}
                        ),
                        **(
                            {
                                "blast_radius": {
                                    k: blast[k]
                                    for k in (
                                        "status",
                                        "summary",
                                        "changed_count",
                                        "affected_files",
                                        "affected_tests",
                                        "rpn",
                                        "risk",
                                    )
                                    if k in blast
                                }
                            }
                            if blast.get("status") == "pass"
                            else {}
                        ),
                    },
                    "checklist_results": chk_results,
                    "scope_violation": scope_violation,
                },
            )
        facts: dict[str, Any] = {
            "machine_gate": mg_fact,
            "mg_failures": mg_failures,  # runner/feedback.py → lý do cụ thể cho lần IMPLEMENT kế
            "regression_gate": regression,
            "test_refs": test_refs,
            **as_scope_facts(blast),
        }
        if chk_facts:
            facts["checklist"] = chk_facts
        if scope_violation:
            facts["touches_outside_scope"] = True
        return StageResult(stage=self.stage, facts=facts)

    # ───── nội bộ ─────
    def _run(self, ctx: StageContext, wt: Path, cmd: str, label: str) -> dict[str, Any]:
        return run_gate_cmd(ctx, wt, cmd, label)

    @staticmethod
    def _spec(ctx: StageContext, issue: str) -> dict[str, Any]:
        try:
            return dict(ctx.svc.store.read(issue, "spec.yaml").content)
        except StoreError as e:
            raise ConfigError("MACHINE_GATE cần spec.yaml") from e

    @staticmethod
    def _slot(ctx: StageContext, issue: str, slot: str) -> dict[str, Any]:
        try:
            return dict(ctx.svc.store.read(issue, slot).content)
        except StoreError:
            return {}

    @staticmethod
    def _repo_md(ctx: StageContext, wt: Path) -> str | None:
        for p in (ctx.svc.layout.memory_dir / "REPO.md", wt / "REPO.md"):
            if p.is_file():
                return p.read_text(encoding=ENCODING)
        return None


def _baseline(state: IssueState) -> set[str]:
    pf = state.facts.get("preflight")
    return {str(x) for x in pf.get("baseline_failures") or []} if isinstance(pf, dict) else set()


def _minus_baseline(g: dict[str, Any], baseline: set[str]) -> dict[str, Any]:
    """Test đỏ sẵn trên base (preflight) không phải lỗi của patch: fail mà MỌI test fail đều thuộc baseline → pass."""
    if g.get("status") != "fail" or not baseline:
        return g
    raw = g.get("parsed")
    parsed: dict[str, Any] = raw if isinstance(raw, dict) else {}
    names = {str(t.get("name")) for t in parsed.get("failed_tests") or [] if isinstance(t, dict)}
    if not names or not names <= baseline or parsed.get("status") not in ("fail",):
        return g
    return {
        **g,
        "status": "pass",
        "summary": f"only pre-existing failures ({len(names)}): {g.get('summary')}"[:200],
    }


def _failure(name: str, g: dict[str, Any], cmd: str | None) -> dict[str, Any]:
    """Một gate fail → đủ để Dev biết sửa gì: lệnh, tóm tắt, test fail (tối đa 10), ref log đầy đủ."""
    raw = g.get("parsed")
    parsed: dict[str, Any] = raw if isinstance(raw, dict) else {}
    tests = [
        {"name": t.get("name"), "message": str(t.get("message") or "")[:200]}
        for t in (parsed.get("failed_tests") or [])[:10]
        if isinstance(t, dict)
    ]
    out: dict[str, Any] = {"gate": name, "summary": str(g.get("summary") or "")[:200], "failed_tests": tests}
    if cmd:
        out["cmd"] = cmd
    if g.get("output_ref"):
        out["output_ref"] = g["output_ref"]
    return out


def run_gate_cmd(ctx: StageContext, wt: Path, cmd: str, label: str) -> dict[str, Any]:
    """Chạy 1 lệnh gate (0 LLM) trong sandbox profile dev, parse → {status, summary, output_ref, parsed}."""
    svc = ctx.svc
    if svc.sandbox is None or svc.sandbox_cfg is None or svc.artifacts is None:
        raise ConfigError("gate cần sandbox", hint="chạy với --repo")
    # lệnh gate do agent viết (spec.test_command) hoặc lấy từ REPO.md: qua cùng bộ kiểm với exec.run, không chạy mù
    verdict = check_command(cmd, ctx.config.policy.command_guard, worktree=str(wt))
    if not verdict.ok:
        return {
            "status": "fail",
            "summary": f"lệnh bị chặn ({verdict.rule}): {verdict.reason}"[:200],
            "guard": verdict.rule,
        }
    prof = profile_for(svc.sandbox_cfg, "dev", wt)
    started = time.time()
    res = svc.sandbox.run(
        cmd, cwd=wt, profile=prof, timeout_s=float(ctx.config.budgets.tools.test), cancel=ctx.cancel
    )
    stdout, stderr = read_output(res)
    ref = svc.artifacts.put(
        ctx.issue_id, f"$ {cmd}\n{stdout}\n--- stderr ---\n{stderr}", ext="log", label=f"mg-{label}"
    )
    if res.timed_out:
        return {"status": "fail", "summary": f"timeout {cmd[:40]}", "output_ref": ref}
    runner = detect_runner(cmd)
    parsed = parse_output(runner, stdout, stderr, res.exit_code)
    if runner in JUNIT_REPORT_DIRS and not (parsed.get("passed") or parsed.get("failed")):
        rep = read_junit_reports(wt, JUNIT_REPORT_DIRS[runner], since=started - 1)
        if rep is not None:
            parsed.update(rep)
    if parsed["status"] == "pass":
        status = "pass"
    elif parsed["status"] in ("fail", "error"):
        status = "fail"
    else:
        status = "fail" if res.exit_code else "pass"
    return {
        "status": status,
        "summary": str(parsed.get("summary_line") or f"exit {res.exit_code}")[:200],
        "output_ref": ref,
        "parsed": parsed,
    }

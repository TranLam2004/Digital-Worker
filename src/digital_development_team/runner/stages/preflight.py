"""Preflight của IMPLEMENT — 0 LLM, chạy trước Dev ở lần đầu (và mỗi khi spec đổi version).

SPEC chỉ có ~30 giây nên không chạy test được ở đó; IMPLEMENT có vài phút và worktree. Kiểm:
  lệnh        test_command / regression_command qua command guard; chương trình có trên máy
  đường dẫn   file/thư mục mà lệnh nhắc tới phải tồn tại HOẶC nằm trong allowed_files (Dev sẽ tạo — test viết trước)
  baseline    chạy regression_command trên base: test đã đỏ sẵn → facts.preflight.baseline_failures; Dev được báo,
              machine gate không tính các lỗi đó là lỗi của patch
Kết quả: status ok · bad_spec (lệnh trong spec không dùng được → transition về SPEC một lần) · environment (thiếu
chương trình trên máy → không tốn lượt Dev, báo người).

Baseline cache theo (SHA base, lệnh) ở memory/baseline_cache.json: cùng commit main thì kết quả giống hệt — issue
sau dùng lại thay vì chạy cả bộ regression trước mỗi lần Dev làm. MERGE nạp sẵn cho commit main mới.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import StageResult
from digital_development_team.policy.command import check_command, prog_name, split_segments, tokenize, unwrap
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.agent_stage import ensure_worktree
from digital_development_team.runner.stages.base import StageHandler
from digital_development_team.runner.stages.machine_gate import run_gate_cmd
from digital_development_team.runner.state import IssueState
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.globs import GlobSet

FACT = "preflight"
_PATH_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".cs", ".go", ".rb", ".php", ".rs")
_NOT_FOUND_EXIT = {127, 9009}
MAX_BASELINE = 50
BASELINE_CACHE = "baseline_cache.json"
_CACHE_MAX = 200


def _cache_path(layout: HomeLayout) -> Path:
    return layout.memory_dir / BASELINE_CACHE


def baseline_cache_get(layout: HomeLayout, sha: str, cmd: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path(layout).read_text(encoding=ENCODING))
    except (OSError, ValueError):
        return None
    hit = data.get(f"{sha}|{cmd}") if isinstance(data, dict) else None
    return hit if isinstance(hit, dict) else None


def baseline_cache_put(layout: HomeLayout, sha: str, cmd: str, failures: list[str], note: str = "") -> None:
    """Ghi kết quả baseline của regression trên commit `sha`. Lỗi I/O không chặn pipeline."""
    path = _cache_path(layout)
    try:
        data = json.loads(path.read_text(encoding=ENCODING)) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[f"{sha}|{cmd}"] = {"baseline_failures": failures[:MAX_BASELINE], "note": note[:200]}
    if len(data) > _CACHE_MAX:  # giữ các mục mới nhất (dict giữ thứ tự chèn)
        data = dict(list(data.items())[-_CACHE_MAX:])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(data, ensure_ascii=False), fsync=False)
    except OSError:
        pass


class ImplementPreflight:
    stage = Stage.IMPLEMENT
    uses_llm = True

    def __init__(self, inner: StageHandler) -> None:
        self._inner = inner
        self.role = getattr(inner, "role", None)

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        pick = getattr(self._inner, "model_role_for", None)
        return str(pick(state, cfg)) if callable(pick) else str(self.role)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        ref = ctx.svc.store.latest(state.issue_id, "spec.yaml")
        pf = state.facts.get(FACT)
        stale = not isinstance(pf, dict) or ref is None or pf.get("spec_version") != ref.version
        if ref is not None and ctx.svc.worktrees is not None and ctx.svc.sandbox is not None and stale:
            pf = run_preflight(state, ctx, ref.version)
            if pf["status"] != "ok":
                reason = f"preflight {pf['status']}: " + "; ".join(pf["problems"])
                return StageResult(
                    stage=self.stage,
                    facts={FACT: pf, "outcome": {"kind": "blocked", "uri": None, "reason": reason[:600]}},
                )
            state.facts[FACT] = pf  # brief của Dev đọc baseline_failures
        res = self._inner.run(state, ctx)
        if isinstance(pf, dict):
            return res.model_copy(update={"facts": {**res.facts, FACT: pf}})
        return res


def run_preflight(state: IssueState, ctx: StageContext, spec_version: int) -> dict[str, Any]:
    ensure_worktree(state, ctx, Stage.IMPLEMENT)
    wt = Path(str(state.worktree))
    try:
        spec = dict(ctx.svc.store.read(state.issue_id, "spec.yaml").content)
    except StoreError:
        return {"status": "ok", "spec_version": spec_version, "problems": [], "baseline_failures": []}
    allowed = [str(x) for x in spec.get("allowed_files", [])]
    problems: list[str] = []
    from digital_development_team.runner.contest_validation import mvs_mapping_problems
    from digital_development_team.runner.match import read_match

    problems.extend(mvs_mapping_problems(read_match(ctx.svc, state.issue_id), spec))
    env: list[str] = []
    try:
        task_type = str(ctx.svc.store.read(state.issue_id, "intake.yaml").content.get("type") or "")
    except StoreError:
        task_type = ""
    if ctx.config.policy.task_profile(task_type).test_command == "required" and not spec.get("test_command"):
        problems.append(f"test_command is required for task type `{task_type or 'default'}`")
    commands = [(key, spec.get(key)) for key in ("test_command", "regression_command")]
    commands += [
        (f"{ac['id']}.test_command", ac.get("test_command"))
        for ac in spec.get("acceptance_criteria", [])
        if ac.get("mvs_ids")
    ]
    for key, value in commands:
        cmd = str(value or "").strip()
        if not cmd:
            continue
        verdict = check_command(cmd, ctx.config.policy.command_guard, worktree=str(wt))
        if not verdict.ok:
            problems.append(
                f"{key} `{cmd}` is blocked by the command guard ({verdict.rule}): {verdict.reason}"
            )
            continue
        p, e = _inspect(cmd, wt, allowed)
        problems += [f"{key}: {x}" for x in p]
        env += [f"{key}: {x}" for x in e]
    baseline: list[str] = []
    note = ""
    reg = str(spec.get("regression_command") or "").strip()
    cached = (
        baseline_cache_get(ctx.svc.layout, state.base_commit, reg)
        if reg and state.base_commit and not problems and not env
        else None
    )
    if cached is not None:
        baseline = [str(x) for x in cached.get("baseline_failures") or []]
        note = str(cached.get("note") or "")
    elif reg and not problems and not env:
        r = run_gate_cmd(ctx, wt, reg, "baseline")
        raw = r.get("parsed")
        parsed: dict[str, Any] = raw if isinstance(raw, dict) else {}
        exit_code = parsed.get("exit_code")
        if exit_code in _NOT_FOUND_EXIT:
            env.append(f"regression_command `{reg}`: program not found (exit {exit_code})")
        elif parsed.get("status") in ("error", "no_tests"):
            problems.append(
                f"regression_command `{reg}` does not run on the base commit "
                f"({parsed.get('status')}): {r.get('summary')}"
            )
        elif r.get("status") == "fail":
            baseline = [
                str(t.get("name"))
                for t in parsed.get("failed_tests") or []
                if isinstance(t, dict) and t.get("name")
            ][:MAX_BASELINE]
            note = str(r.get("summary") or "")[:200]
        if state.base_commit and not env and not problems:
            baseline_cache_put(ctx.svc.layout, state.base_commit, reg, baseline, note)
    status = "bad_spec" if problems else ("environment" if env else "ok")
    out: dict[str, Any] = {
        "status": status,
        "spec_version": spec_version,
        "problems": problems + env,
        "baseline_failures": baseline,
    }
    if note:
        out["baseline_summary"] = note
    return out


def _inspect(cmd: str, wt: Path, allowed: list[str]) -> tuple[list[str], list[str]]:
    """(vấn đề của spec, vấn đề môi trường) cho một lệnh đã qua guard."""
    tokens = tokenize(cmd) or []
    problems: list[str] = []
    env: list[str] = []
    globs = GlobSet(allowed) if allowed else None
    current = wt.resolve()
    for seg, _ in split_segments(tokens):
        argv = unwrap(seg)
        if not argv:
            continue
        prog = argv[0]
        if prog_name(prog) == "cd" and len(argv) >= 2:
            target = (current / argv[-1]).resolve()
            if not target.is_relative_to(wt.resolve()) or not target.is_dir():
                problems.append(f"cd target `{argv[-1]}` must be an existing folder inside the worktree")
            else:
                current = target
            continue
        if "/" in prog or "\\" in prog:
            if not (current / prog).exists():
                problems.append(f"program `{prog}` does not exist in the repo")
        elif shutil.which(prog, path=os.environ.get("PATH")) is None and prog_name(prog) not in ("cd", "set"):
            env.append(f"program `{prog}` is not installed on this machine")
        for tok in argv[1:]:
            path = _path_arg(tok)
            if path is None or (current / path).exists():
                continue
            candidate = (current / path).resolve()
            relative = (
                candidate.relative_to(wt.resolve()).as_posix()
                if candidate.is_relative_to(wt.resolve())
                else ""
            )
            if (
                relative
                and globs is not None
                and (globs.match(relative) or any(relative == a for a in allowed))
            ):
                continue  # Dev sẽ tạo (test viết trước)
            problems.append(f"`{path}` does not exist and is not in allowed_files")
    return problems, env


def _path_arg(tok: str) -> str | None:
    if tok.startswith("-") or "=" in tok:
        return None
    path = tok.split("::", 1)[0].split("[", 1)[0].replace("\\", "/")
    if not path or path.startswith(("http:", "https:")):
        return None
    if "/" in path or path.endswith(_PATH_EXT):
        return path.rstrip("/") or None
    return None

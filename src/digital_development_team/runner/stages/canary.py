"""Mutation canary cho Verifier (Design §13.5, JD Verifier "Mutation canary — bạn không biết TASK NÀO").

~rate task T1/T2 (policy.canary), harness tiêm một lỗi nhân tạo vào dòng vừa đổi trong worktree trước khi Verifier
chạy: đảo điều kiện, lệch một đơn vị, bỏ kiểm None, đảo return, xoá assert. Verifier chạy như mọi lần (không biết task
nào có canary) → harness khôi phục file từ git → ghi bắt được hay không (canary_results) → chạy Verifier lần nữa trên
code thật để lấy evidence dùng cho gate. Tỷ lệ bắt < ngưỡng (≥3 lần) → tier_rules nâng +1 mọi task Verifier duyệt.

Chọn task và dòng tất định theo issue id (chạy lại cho cùng kết quả). 0 LLM ngoài chính các lần chạy Verifier.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import DDTError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import StageResult
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.base import StageHandler, inner_model_role
from digital_development_team.runner.state import IssueState
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.hashing import sha256_text

_CMP = [("==", "!="), ("!=", "=="), ("<=", ">"), (">=", "<"), (" < ", " >= "), (" > ", " <= ")]


def _flip_condition(line: str) -> str | None:
    if not re.search(r"\b(if|elif|while|return|assert)\b", line):
        return None
    for a, b in _CMP:
        if a in line and "->" not in line and "=>" not in line:
            return line.replace(a, b, 1)
    if re.search(r"\bif not ", line):
        return line.replace("if not ", "if ", 1)
    return None


def _off_by_one(line: str) -> str | None:
    code = line.split("#", 1)[0]
    if '"' in code or "'" in code:
        return None
    m = re.search(r"(?<![\w.])(\d+)(?![\w.])", code)
    if m is None:
        return None
    return line[: m.start(1)] + str(int(m.group(1)) + 1) + line[m.end(1) :]


def _remove_null_check(line: str) -> str | None:
    m = re.match(r"^(\s*)(if|elif)\s+(not\s+\w+|\w+\s+is\s+None|\w+\s+==\s+None)\s*:", line)
    if m is None:
        return None
    return f"{m.group(1)}{m.group(2)} False:" + line[m.end() :]


def _swap_return(line: str) -> str | None:
    for a, b in (("return True", "return False"), ("return False", "return True"), ("return 0", "return 1")):
        if re.search(rf"{re.escape(a)}\b", line):
            return line.replace(a, b, 1)
    return None


def _delete_assert(line: str) -> str | None:
    m = re.match(r"^(\s*)assert\b", line)
    return f"{m.group(1)}pass  # (đã xoá)" if m else None


MUTATIONS = {
    "flip_condition": _flip_condition,
    "off_by_one": _off_by_one,
    "remove_null_check": _remove_null_check,
    "swap_return": _swap_return,
    "delete_assert": _delete_assert,
}


@dataclass(frozen=True, slots=True)
class CanaryPlan:
    mutation: str
    file: str
    line_no: int
    original: str
    mutated: str


def added_lines(diff: str) -> list[tuple[str, str]]:
    """[(file, dòng thêm)] từ unified diff; bỏ file test (canary là lỗi trong code, không trong test)."""
    out: list[tuple[str, str]] = []
    current: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = path[2:] if path.startswith("b/") else (None if path == "/dev/null" else path)
            continue
        if current and raw.startswith("+") and not raw.startswith("+++"):
            low = current.lower()
            if "test" in low.rsplit("/", 1)[-1] or "/tests/" in f"/{low}":
                continue
            out.append((current, raw[1:]))
    return out


def _seed(issue: str, attempt: int) -> int:
    return int(sha256_text(f"{issue}:{attempt}").rsplit(":", 1)[-1][:8], 16)


def should_inject(issue: str, attempt: int, *, rate: float) -> bool:
    return (_seed(issue, attempt) % 1000) < int(rate * 1000)


def plan_canary(
    issue: str, attempt: int, diff: str, worktree: Path, mutations: Sequence[str]
) -> CanaryPlan | None:
    """Chọn dòng và kiểu lỗi tất định. None khi không có dòng nào áp được."""
    lines = added_lines(diff)
    if not lines:
        return None
    kinds = [m for m in mutations if m in MUTATIONS] or list(MUTATIONS)
    seed = _seed(issue, attempt)
    order = [lines[(seed + i) % len(lines)] for i in range(len(lines))]
    kinds = kinds[seed % len(kinds) :] + kinds[: seed % len(kinds)]
    for file, line in order:
        path = worktree / file
        if not path.is_file():
            continue
        text = path.read_text(encoding=ENCODING)
        file_lines = text.split("\n")
        if line not in file_lines:
            continue
        for kind in kinds:
            mutated = MUTATIONS[kind](line)
            if mutated is not None and mutated != line:
                return CanaryPlan(kind, file, file_lines.index(line) + 1, line, mutated)
    return None


def apply_plan(worktree: Path, plan: CanaryPlan) -> None:
    path = worktree / plan.file
    lines = path.read_text(encoding=ENCODING).split("\n")
    lines[plan.line_no - 1] = plan.mutated
    path.write_text("\n".join(lines), encoding=ENCODING)


class CanaryVerify:
    """Bọc handler VERIFY. Không phải lần canary → chạy y nguyên."""

    stage = Stage.VERIFY
    uses_llm = True

    def __init__(self, inner: StageHandler) -> None:
        self._inner = inner
        self.role = getattr(inner, "role", None)

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        return inner_model_role(self._inner, state, cfg, self.role)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        plan = self._plan(state, ctx)
        if plan is None:
            return self._inner.run(state, ctx)
        svc = ctx.svc
        wt = Path(state.worktree or "")
        apply_plan(wt, plan)
        svc.sink.emit(
            EventDraft(
                issue_id=state.issue_id,
                actor="runner",
                event=EventType.CANARY_INJECTED,
                stage=Stage.VERIFY,
                data={"mutation": plan.mutation, "file": plan.file, "line": plan.line_no},
            )
        )
        try:
            first = self._inner.run(state, ctx)
        finally:
            self._restore(ctx, wt, plan)
        verdict = self._verdict(ctx, state.issue_id)
        caught = verdict != "pass"
        with svc.db.transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO canary_results(issue_id, attempt, mutation, file, line, caught, verdict, at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    state.issue_id,
                    state.attempt,
                    plan.mutation,
                    plan.file,
                    plan.line_no,
                    int(caught),
                    verdict,
                    ctx.clock.now().isoformat(),
                ),
            )
        svc.sink.emit(
            EventDraft(
                issue_id=state.issue_id,
                actor="runner",
                event=EventType.CANARY_RESULT,
                stage=Stage.VERIFY,
                data={"mutation": plan.mutation, "caught": caught, "verdict": verdict},
            )
        )
        state.facts["canary"] = {
            "shown": True,
            "caught": caught,
            "mutation": plan.mutation,
            "attempt": state.attempt,
        }
        real = self._inner.run(state, ctx)  # evidence thật cho gate: chạy lại trên code không có lỗi tiêm
        facts = {**real.facts, "canary": state.facts["canary"]}
        return StageResult(
            stage=Stage.VERIFY, facts=facts, usage=first.usage + real.usage, outcome=real.outcome
        )

    def _plan(self, state: IssueState, ctx: StageContext) -> CanaryPlan | None:
        cfg = ctx.config.policy.canary
        if (
            not cfg.enabled
            or ctx.config.policy.economy_mode
            or state.tier.value not in cfg.tiers
            or state.rework > 0
            or state.facts.get("canary")
            or not state.worktree
            or ctx.svc.worktrees is None
            or not should_inject(state.issue_id, state.attempt, rate=cfg.rate)
        ):
            return None
        try:
            diff = str(ctx.svc.store.read(state.issue_id, "patch/").content.get("diff", ""))
        except StoreError:
            return None
        return plan_canary(state.issue_id, state.attempt, diff, Path(state.worktree), cfg.mutations)

    @staticmethod
    def _restore(ctx: StageContext, wt: Path, plan: CanaryPlan) -> None:
        wm = ctx.svc.worktrees
        restored = False
        if wm is not None:
            with contextlib.suppress(DDTError):
                restored = wm.git.run(["checkout", "HEAD", "--", plan.file], cwd=wt).ok
        if not restored:  # không có git: tự đặt lại dòng gốc
            path = wt / plan.file
            lines = path.read_text(encoding=ENCODING).split("\n")
            if plan.line_no - 1 < len(lines) and lines[plan.line_no - 1] == plan.mutated:
                lines[plan.line_no - 1] = plan.original
                path.write_text("\n".join(lines), encoding=ENCODING)

    @staticmethod
    def _verdict(ctx: StageContext, issue: str) -> str | None:
        try:
            return str(ctx.svc.store.read(issue, "evidence.json").content.get("verdict"))
        except StoreError:
            return None


__all__ = ["MUTATIONS", "CanaryPlan", "CanaryVerify", "plan_canary"]

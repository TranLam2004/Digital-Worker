"""Pha 1 — máy: item regex trên diff + item `verify: test` trên test_refs (số đo từ machine gate: sast_scan, coverage_delta…).
blocking fail → artifact fail (harness ép, không cần agent)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from digital_development_team.checklist.loader import ChecklistItem, normalize_when
from digital_development_team.core.errors import ExpressionError, MissingValue
from digital_development_team.expr import compile_expr

_CMP_RE = re.compile(r"^\s*(==|!=|<=|>=|<|>)\s*-?\d+(\.\d+)?\s*$")


@dataclass(frozen=True, slots=True)
class ItemResult:
    id: str
    result: str  # pass | fail | n/a
    severity: str
    evidence: tuple[str, ...] = ()
    by: str = "harness"

    def to_evidence(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "result": self.result, "by": self.by, "severity": self.severity}
        if self.evidence:
            d["evidence"] = " | ".join(self.evidence)[:300]
        return d


@dataclass
class ChecklistRun:
    results: list[ItemResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # item không phải regex (Verifier/P2)

    @property
    def fails(self) -> list[ItemResult]:
        return [r for r in self.results if r.result == "fail"]

    @property
    def blocking_fail(self) -> bool:
        return any(r.severity == "blocking" for r in self.fails)

    @property
    def major_fails(self) -> int:
        return sum(1 for r in self.fails if r.severity == "major")

    @property
    def clean(self) -> bool:
        """Không blocking fail và < 2 major fail (ngưỡng verdict theo _checklist-system)."""
        return not self.blocking_fail and self.major_fails < 2

    @property
    def verdict(self) -> str:
        return "fail" if (self.blocking_fail or self.major_fails >= 2) else "pass"

    def security_regex_pass(self, sec_ids: Sequence[str]) -> bool:
        ids = set(sec_ids)
        return not any(r.id in ids for r in self.fails)

    def facts(self, sec_ids: Sequence[str]) -> dict[str, Any]:
        return {
            "blocking_fail": self.blocking_fail,
            "clean": self.clean,
            "major_fails": self.major_fails,
            "verdict": self.verdict,
            "security_regex_pass": self.security_regex_pass(sec_ids),
            "ran": len(self.results),
            "failed_ids": [r.id for r in self.fails],
        }


def _scope_text(diff: str, scope: str) -> str:
    if scope == "added_lines":
        return "\n".join(
            line for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
    if scope == "removed_lines":
        return "\n".join(
            line for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
        )
    return diff


def run_test_refs(items: Sequence[ChecklistItem], test_refs: Mapping[str, Any]) -> list[ItemResult]:
    """Item `verify: test`: threshold dạng `>= -2` (so với giá trị scalar) hoặc biểu thức trên dict (`high_findings == 0`).
    Không có test_ref trong test_refs → bỏ qua (giữ skipped), không đoán."""
    out: list[ItemResult] = []
    for item in items:
        if item.verify != "test" or not item.test_ref or not item.threshold or item.test_ref not in test_refs:
            continue
        val = test_refs[item.test_ref]
        th = item.threshold.strip()
        try:
            if _CMP_RE.match(th):
                ok = compile_expr(f"value {th}").as_bool({"value": val})
            else:
                scope = dict(val) if isinstance(val, Mapping) else {"value": val}
                ok = compile_expr(normalize_when(th)).as_bool(scope)
        except (ExpressionError, MissingValue, TypeError):
            continue
        ev = f"{item.test_ref} = {json.dumps(val, ensure_ascii=False)[:100]} · ngưỡng `{th}`"
        out.append(ItemResult(item.id, "pass" if ok else "fail", item.severity, (ev,)))
    return out


def run_regex(
    items: Sequence[ChecklistItem], diff: str, *, file_contents: dict[str, str] | None = None
) -> ChecklistRun:
    run = ChecklistRun()
    for item in items:
        if item.verify != "regex" or not item.pattern:
            run.skipped.append(item.id)
            continue
        if item.pattern_scope == "file_content":
            text = "\n".join((file_contents or {}).values())
        else:
            text = _scope_text(diff, item.pattern_scope)
        hits = [m.group(0).strip()[:120] for m in re.finditer(item.pattern, text, re.M)]
        run.results.append(ItemResult(item.id, "fail" if hits else "pass", item.severity, tuple(hits[:3])))
    return run

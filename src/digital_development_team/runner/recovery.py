"""Thang phục hồi + báo cáo BLOCKED — 0 LLM.

Sắp BLOCKED (luật transition không `final`, hết giờ stage, hết budget):
  1. classify_failure → một lớp lỗi tất định từ stage hỏng, lý do, facts, slot và các lần bị policy từ chối gần nhất
  2. bậc đầu tiên trong policy.yaml#recovery.ladder còn lượt và khớp lớp lỗi → issue quay lại stage đích với
     retry_feedback (vì sao hỏng + bậc phục hồi), có thể kèm model mạnh hơn ({role}_strong)
  3. không còn bậc nào → BLOCKED kèm blocked_report.json: category, đã thử gì, phần việc giữ lại, cách gỡ
Lớp cần người (policy_denied, destructive, needs_info, needs_decision, environment, human_abort, cancelled) không có
bậc nào: báo người ngay, đúng tinh thần "chỉ báo lên khi thật sự bị chặn".
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from digital_development_team.config.models import RecoveryCfg, RecoveryRungCfg
from digital_development_team.core.enums import Stage

SlotReader = Callable[[str], Mapping[str, Any] | None]

AGENT_STAGE_ROLE: dict[Stage, str] = {
    Stage.TRIAGE: "intake",
    Stage.LOCALIZE: "planner",
    Stage.SPEC: "analyst",
    Stage.IMPLEMENT: "dev",
    Stage.ANSWER: "dev",
    Stage.GATE: "leader",
}

CATEGORY: dict[str, str] = {
    "policy_denied": "policy",
    "destructive": "needs_access",
    "scope": "needs_access",
    "needs_info": "needs_info",
    "needs_decision": "needs_decision",
    "human_abort": "cancelled",
    "cancelled": "cancelled",
    "environment": "environment",
    "budget_stage": "budget",
    "budget_issue": "budget",
    "too_large": "budget",
    "deadline": "budget",  # quá hạn item (thi đấu): bỏ item, không phục hồi
}
"""Lớp lỗi → loại giúp đỡ người cần làm. Lớp không có ở đây → capability (agent đã thử hết cách)."""

_NEEDS_INFO = re.compile(
    r"(?i)(unclear|ambigu|missing (information|requirement)|need(s)? (more )?(info|clarification|details)|"
    r"không rõ|thiếu thông tin|mơ hồ)"
)
_NO_OUTPUT = re.compile(r"(?i)(không ghi|without writing|did not write|no output|kết thúc mà không)")
_POLICY_DENY = re.compile(r"(?i)(secret|sensitive|git internals|no agent may push|destructive|G2)")
_SCOPE_DENY = re.compile(r"(?i)(scope violation|outside spec\.allowed_files|outside the allowlist)")


@dataclass(frozen=True, slots=True)
class Plan:
    """Bậc phục hồi được chọn."""

    rung: str
    failure_class: str
    to: Stage
    model_role: str | None = None
    mode: str | None = None


def _m(x: Any) -> Mapping[str, Any]:
    return x if isinstance(x, Mapping) else {}


def classify_failure(
    *,
    from_stage: Stage,
    reason: str,
    facts: Mapping[str, Any],
    read_slot: SlotReader,
    denials: Sequence[str] = (),
) -> str:
    """Một lớp trong config.models.FAILURE_CLASSES. Thứ tự luật = ưu tiên."""
    r = reason or ""
    gate = _m(facts.get("gate"))
    pf = _m(facts.get("preflight"))
    if facts.get("cancelled"):
        return "cancelled"
    if from_stage in (Stage.G0_WAIT, Stage.G1_WAIT) and gate:
        if gate.get("route") == "abort" or gate.get("choice") == "reject_final":
            return "human_abort"
        if gate.get("choice") == "reject" and not gate.get("is_timeout") and from_stage is Stage.G0_WAIT:
            return "human_abort"  # từ chối trước khi code = không làm
    if pf.get("status") == "environment" or r.startswith("preflight environment"):
        return "environment"
    if pf.get("status") == "bad_spec" or r.startswith("preflight bad_spec"):
        return "test_command"
    if "TOO_MANY_ASKS" in r:
        return "needs_decision"
    if r.startswith("BUDGET") or "hết budget" in r or "budget hết" in r:
        return "budget_issue" if "issue" in r.lower() else "budget_stage"
    if "STUCK" in r:
        return "stuck"
    merge = facts.get("merge")
    if from_stage is Stage.MERGE and isinstance(merge, Mapping) and merge.get("conflict"):
        return "base_moved"
    if from_stage is Stage.MACHINE_GATE:
        mg = facts.get("machine_gate")
        if isinstance(mg, Mapping) and mg.get("scope_violation"):
            return "scope"
        return "test_fail"
    if from_stage in (Stage.VERIFY, Stage.REVIEW):
        ev = read_slot("evidence.json") or {}
        out = _m(facts.get("outcome"))
        if out.get("kind") == "blocked":
            return "verifier_unavailable"
        return "test_fail" if ev.get("verdict") == "fail" else "spec_conflict"
    if from_stage is Stage.G1_WAIT:
        return "test_fail"  # người/timeout trả về nhiều lần mà Dev chưa sửa được
    if from_stage is Stage.GATE:
        dec = read_slot("decision.json") or {}
        if dec.get("action") == "block":
            return "needs_decision"
    if any(_SCOPE_DENY.search(d) for d in denials):
        return "scope"
    if any(_POLICY_DENY.search(d) for d in denials):
        return "policy_denied"
    if "refusal" in r or "từ chối" in r:
        return "needs_decision"
    if _NO_OUTPUT.search(r):
        return "no_output"
    if _NEEDS_INFO.search(r):
        return "needs_info"
    return "agent_gave_up"


def choose(
    cfg: RecoveryCfg,
    *,
    failure_class: str,
    from_stage: Stage,
    counts: Mapping[str, int],
    model_roles: Sequence[str],
    split_enabled: bool = True,
) -> Plan | None:
    """Bậc đầu tiên còn lượt và khớp lớp lỗi; None = báo người. SPLIT tắt (thi đấu) → bỏ bậc decompose."""
    if not cfg.enabled:
        return None
    for rung in cfg.ladder:
        if failure_class not in rung.classes or int(counts.get(rung.rung, 0)) >= rung.max:
            continue
        if rung.rung == "decompose" and not split_enabled:
            continue
        if rung.stages is not None and from_stage.value not in rung.stages:
            continue
        to = _target(rung, from_stage)
        if to is None:
            continue
        model_role = None
        if rung.model_role:
            role = AGENT_STAGE_ROLE.get(to, "")
            model_role = rung.model_role.replace("{role}", role)
            if not role or model_role not in model_roles:
                continue  # model mạnh hơn cho role này chưa cấu hình → bậc không áp được
        return Plan(rung.rung, failure_class, to, model_role, rung.mode)
    return None


def _target(rung: RecoveryRungCfg, from_stage: Stage) -> Stage | None:
    if rung.to == "auto":
        return from_stage if from_stage in AGENT_STAGE_ROLE else Stage.IMPLEMENT
    try:
        return Stage(rung.to)
    except ValueError:
        return None


def recovery_note(plan: Plan, reason: str) -> str:
    """Dòng thêm vào retry_feedback để agent biết đây là bậc phục hồi, không phải lần chạy thường."""
    how = {
        "retry_informed": "Previous attempt ended without a usable result. Read the reason above and finish the task.",
        "re_spec": "The spec itself was the problem. Rewrite it so the work can be done and verified.",
        "re_localize": "Fixes at the planned location kept failing. Look for a different location or approach.",
        "model_upgrade": "Earlier attempts with a smaller model failed. Diagnose WHY they failed before editing.",
        "decompose": "The task is too large for one change. Split it: fill plan.subtasks with 2-5 parts, each "
        "testable on its own, in the order they must be done.",
    }.get(plan.rung, "")
    return f"- recovery step `{plan.rung}` after `{plan.failure_class}`: {reason[:300]}\n  {how}".rstrip()


# ───── báo cáo BLOCKED ─────
def category_of(failure_class: str) -> str:
    return CATEGORY.get(failure_class, "capability")


def build_blocked_report(
    *,
    issue: str,
    failure_class: str,
    from_stage: Stage,
    reason: str,
    hint: str | None,
    facts: Mapping[str, Any],
    rework: int,
    reloc: int,
    branch: str | None,
    worktree: str | None,
    files_touched: Sequence[str],
    spec_version: int | None,
    reopens: int,
    max_reopens: int,
    at: str,
    progress: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    category = category_of(failure_class)
    tried: list[dict[str, Any]] = []
    if rework:
        tried.append({"step": f"rework x{rework}", "stage": "IMPLEMENT"})
    if reloc:
        tried.append({"step": f"re_localize x{reloc}", "stage": "LOCALIZE"})
    rec = _m(facts.get("recovery"))
    for h in rec.get("history") or []:
        if isinstance(h, Mapping):
            tried.append(
                {
                    "step": f"recovery:{h.get('rung')}",
                    "stage": str(h.get("to") or ""),
                    "result": f"after {h.get('class')}",
                }
            )
    fb = _m(facts.get("retry_feedback"))
    refs = [str(x) for x in fb.get("refs") or []]
    out = _m(facts.get("outcome"))
    if out.get("uri"):
        refs.append(str(out["uri"]))
    resumable = category != "cancelled" and reopens < max_reopens
    options = _options(category, from_stage, resumable)
    report: dict[str, Any] = {
        "issue_id": issue,
        "category": category,
        "failure_class": failure_class,
        "stage": from_stage.value,
        "summary": reason[:600] or "blocked",
        "tried": tried,
        "evidence_refs": refs[:10],
        "partial": {
            "branch": branch,
            "worktree": worktree,
            "files_touched": list(files_touched)[:50],
            "spec_version": spec_version,
        },
        "unblock_options": options,
        "recommendation": options[0]["id"],
        "resumable": resumable,
        "reopens": reopens,
        "at": at,
    }
    if progress:
        report["partial"]["progress"] = {
            k: progress[k] for k in ("items", "learned", "next") if k in progress
        }
    if hint:
        report["hint"] = hint[:300]
    return report


def _options(category: str, stage: Stage, resumable: bool) -> list[dict[str, Any]]:
    at = stage.value if stage in AGENT_STAGE_ROLE else "IMPLEMENT"
    if not resumable:
        return [{"id": "A", "action": "Close the issue (no further reopen allowed)"}]
    table: dict[str, list[dict[str, Any]]] = {
        "environment": [
            {"id": "A", "action": "Install the missing tool, then reopen", "reopen_at": "IMPLEMENT"},
            {
                "id": "B",
                "action": "Fix spec commands to use tools that exist, then reopen",
                "reopen_at": "SPEC",
            },
        ],
        "budget": [
            {
                "id": "A",
                "action": "Reopen with +50% budget",
                "reopen_at": at,
                "grant": {"budget_pct": 50},
            },
            {"id": "B", "action": "Split the issue into smaller backlog items"},
        ],
        "needs_access": [
            {"id": "A", "action": "Allow the needed files (reopen --allow PATH)", "reopen_at": "IMPLEMENT"},
            {"id": "B", "action": "Reopen at SPEC so the Analyst re-scopes the change", "reopen_at": "SPEC"},
        ],
        "policy": [
            {
                "id": "A",
                "action": "Review the denied action; if it is legitimate, adjust policy and reopen",
                "reopen_at": at,
            },
            {"id": "B", "action": "Close the issue"},
        ],
        "needs_info": [
            {"id": "A", "action": "Answer the open question in the reopen note", "reopen_at": at},
            {"id": "B", "action": "Close the issue"},
        ],
        "needs_decision": [
            {"id": "A", "action": "Decide and reopen with the decision in the note", "reopen_at": at},
            {"id": "B", "action": "Close the issue"},
        ],
        "capability": [
            {"id": "A", "action": "Reopen with a hint in the note", "reopen_at": "IMPLEMENT"},
            {"id": "B", "action": "Reopen at LOCALIZE for a different approach", "reopen_at": "LOCALIZE"},
        ],
    }
    return table.get(category, [{"id": "A", "action": "Close the issue"}])

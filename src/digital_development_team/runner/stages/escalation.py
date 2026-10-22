"""Escalation trong pipeline (70-human-control/04-escalation.md).

Agent nộp escalation.yaml thay cho artifact → harness mở gate người → câu trả lời quay về đúng stage đã hỏi.

  gate     trước bước code (TRIAGE/LOCALIZE/SPEC) luôn G0 · IMPLEMENT theo form, mặc định G1 · sau đó G1.
           G2 chưa có pause riêng → đi G1, không timeout (hành động phá huỷ không có mặc định).
  giới hạn mỗi role hỏi tối đa MAX_ASKS lần/issue. Lần sau đó harness tự chọn khuyến nghị (TOO_MANY_ASKS),
           agent chạy lại với câu trả lời đó; hỏi tiếp nữa → blocked.
  trả lời  escalation.yaml bản mới có `resolved` · facts.human_answers (agents/brief.py đưa vào CONTEXT) ·
           human_override khi người chọn khác khuyến nghị.
  route    option có thể ghi `route`: continue (mặc định: chạy lại stage đã hỏi) · rework · re_localize ·
           merge (chỉ G1) · abort. Approve trên card escalation = chọn khuyến nghị; reject = abort.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from digital_development_team.blackboard.store import FileStore
from digital_development_team.core.context import Actor
from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import StoreError

SLOT = "escalation.yaml"
MAX_ASKS = 2
G0_STAGES = frozenset({Stage.TRIAGE, Stage.LOCALIZE, Stage.SPEC, Stage.ANSWER})
ROUTES = ("continue", "rework", "re_localize", "re_spec", "merge", "abort")


def read_escalation(store: FileStore, issue: str) -> dict[str, Any] | None:
    try:
        return dict(store.read(issue, SLOT).content)
    except StoreError:
        return None


def gate_for(esc: Mapping[str, Any], stage: Stage) -> str:
    """Trước bước code luôn G0 · IMPLEMENT theo form (mặc định G1) · từ VERIFY trở đi luôn G1.
    G2 chưa có pause riêng → G1 (không timeout, xem `destructive`)."""
    if stage in G0_STAGES:
        return "G0"
    if stage is Stage.IMPLEMENT and esc.get("gate") == "G0":
        return "G0"
    return "G1"


def raised_facts(
    facts: Mapping[str, Any], esc: Mapping[str, Any], *, stage: Stage, role: str
) -> dict[str, Any]:
    """Facts khi agent vừa nộp escalation. `escalated.count` = số lần role này đã hỏi trong issue (kể cả lần này)."""
    asks = dict(facts.get("asks") or {})
    asks[role] = int(asks.get(role, 0)) + 1
    options = [str(o.get("id")) for o in esc.get("options", []) if isinstance(o, Mapping)]
    return {
        "asks": asks,
        "escalation_pending": True,
        "escalated": {
            "gate": gate_for(esc, stage),
            "type": str(esc.get("type") or ""),
            "raised_by": role,
            "stage": stage.value,
            "question": str(esc.get("question") or "")[:400],
            "recommendation": str(esc.get("recommendation") or ""),
            "options": options,
            "count": asks[role],
            "destructive": esc.get("type") == "destructive_request",
        },
    }


@dataclass(frozen=True, slots=True)
class Resolution:
    choice: str  # A-D · other · reject
    route: str  # ROUTES
    chosen_action: str
    recommendation: str
    override: bool  # người chọn option khác khuyến nghị
    return_to: str  # stage đã hỏi


def resolve(esc: Mapping[str, Any], choice: str, note: str | None, *, stage: str) -> Resolution:
    """Lựa chọn của người (hoặc của harness khi timeout) → hành động cụ thể. Không đoán: option không ghi route = continue."""
    rec = str(esc.get("recommendation") or "")
    opts = {str(o.get("id")): o for o in esc.get("options", []) if isinstance(o, Mapping)}
    if choice == "approve":
        choice = rec
    if choice in ("reject", "reject_final"):
        return Resolution("reject", "abort", note or "người từ chối", rec, False, stage)
    if choice == "other":
        return Resolution("other", "continue", note or "", rec, True, stage)
    opt = opts.get(choice)
    if opt is None:  # option không tồn tại (form hỏng sau khi mở ticket) → an toàn: dừng
        return Resolution(choice, "abort", f"option {choice} không có trong form", rec, False, stage)
    route = str(opt.get("route") or "continue")
    if route not in ROUTES:
        route = "continue"
    return Resolution(choice, route, str(opt.get("action") or ""), rec, choice != rec, stage)


G2_CHOICES = frozenset({"allow_once", "allow_issue", "deny"})


def resolve_g2(
    esc: Mapping[str, Any], choice: str, note: str | None, *, stage: str, g2: Mapping[str, Any]
) -> Resolution:
    """Quyết định trên card G2: cho phép đúng lời gọi đã bị chặn (một lần / cả issue) hoặc từ chối. Agent chạy lại stage."""
    target = f"{g2.get('tool')} {g2.get('target') or ''}".strip()
    if choice == "deny":
        action = (
            f"không được chạy {target}" + (f": {note}" if note else "") + ". Tìm cách khác hoặc báo blocked."
        )
    else:
        scope = "một lần" if choice == "allow_once" else "trong cả issue"
        action = f"được phép chạy đúng lời gọi {target} ({scope}). Chạy lại đúng lời gọi đó."
    return Resolution(choice, "continue", action, str(esc.get("recommendation") or ""), False, stage)


def answer_entry(
    esc: Mapping[str, Any], res: Resolution, *, by: str, note: str | None, wait_sec: int | None
) -> dict[str, Any]:
    return {
        "stage": res.return_to,
        "raised_by": str(esc.get("raised_by") or ""),
        "question": str(esc.get("question") or ""),
        "choice": res.choice,
        "chosen_action": res.chosen_action,
        "route": res.route,
        "by": by,
        "note": note,
        "wait_sec": wait_sec,
        "override": res.override,
    }


def write_resolved(
    store: FileStore,
    issue: str,
    esc: Mapping[str, Any],
    res: Resolution,
    *,
    by: str,
    at: datetime,
    note: str | None,
    is_timeout: bool,
) -> None:
    """escalation.yaml bản mới có `resolved`. Người quyết → ghi dưới tên người; timeout/tự quyết → runner."""
    body = {k: v for k, v in esc.items() if k != "resolved"}
    resolved: dict[str, Any] = {
        "by": by,
        "at": at.isoformat(),
        "choice": "timeout" if is_timeout else res.choice,
        "chosen_action": res.chosen_action[:200],
        "route": res.route,
    }
    if note:
        resolved["note"] = note[:300]
    actor = Actor(role=by) if by.startswith("human") else Actor(role="runner")
    store.write(issue, SLOT, {**body, "resolved": resolved}, actor=actor)


def on_escalation(
    state: Any, store: FileStore, *, now: datetime, stage: Stage, role: str
) -> dict[str, Any] | None:
    """Agent vừa nộp escalation. Trong giới hạn → facts để transitions mở gate. Quá giới hạn → harness tự chọn
    khuyến nghị, ghi câu trả lời vào state, trả None để stage chạy lại agent một lần."""
    issue = state.issue_id
    esc = read_escalation(store, issue)
    if esc is None:  # loop báo escalation mà không đọc được form → dừng, không đoán
        return {"outcome": {"kind": "blocked", "uri": None, "reason": "escalation.yaml không đọc được"}}
    raised = raised_facts(state.facts, esc, stage=stage, role=role)
    if int(raised["escalated"]["count"]) <= MAX_ASKS:
        return raised
    note = f"TOO_MANY_ASKS: {role} đã hỏi {MAX_ASKS} lần, harness chọn khuyến nghị"
    res = resolve(esc, "approve", note, stage=stage.value)
    write_resolved(store, issue, esc, res, by="system", at=now, note=note, is_timeout=False)
    answers = list(state.facts.get("human_answers") or [])
    answers.append(answer_entry(esc, res, by="system", note=note, wait_sec=0))
    state.merge_facts({"asks": raised["asks"], "human_answers": answers})
    return None


__all__ = [
    "G2_CHOICES",
    "MAX_ASKS",
    "ROUTES",
    "Resolution",
    "answer_entry",
    "gate_for",
    "on_escalation",
    "raised_facts",
    "read_escalation",
    "resolve",
    "resolve_g2",
    "write_resolved",
]

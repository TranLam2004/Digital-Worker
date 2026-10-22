"""Quyết định gate — logic dùng chung cho CLI (`ddt resolve`) và web (`POST /api/gates/{id}/resolve`).

Một chỗ duy nhất biết: gate nào nhận lựa chọn nào, lựa chọn nào bắt buộc lý do, batch approve áp cho tier nào.
Web không được import cli, nên phần này nằm ở gates (L4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from digital_development_team.core.context import Clock
from digital_development_team.core.enums import GateId
from digital_development_team.core.errors import DDTError, StoreError
from digital_development_team.core.models import GateDecision
from digital_development_team.gates.service import GateService, OpenGate


class GateError(DDTError):
    code = "GATE"


ESCALATION_CHOICES = frozenset({"A", "B", "C", "D", "other"})
"""Lựa chọn cho card escalation (G0 hoặc G1): option id A-D hoặc 'other' kèm note. Chỉ hợp lệ khi ticket có form.
Trên card escalation, approve = chọn khuyến nghị, reject = dừng issue (runner/stages/escalation.py)."""

ALLOWED: dict[GateId, frozenset[str]] = {
    GateId.G0: frozenset({"approve", "reject"}) | ESCALATION_CHOICES,
    GateId.G1: frozenset({"approve", "reject", "reject_final"}) | ESCALATION_CHOICES,
    GateId.G2: frozenset({"allow_once", "allow_issue", "deny"}),  # duyệt đúng một lời gọi tool phá huỷ
    GateId.G3: frozenset(),
}

NOTE_REQUIRED = frozenset({"reject", "reject_final", "other", "deny"})
"""Người dạy hệ thống bằng lý do, không sửa code — reject không có lý do là vô nghĩa với Dev và Learner."""

REJECT_REASONS: tuple[str, ...] = (
    "Sai acceptance criteria",
    "Thiếu test / test không chứng minh được",
    "Đụng file ngoài phạm vi",
    "Rủi ro bảo mật",
    "Sửa sai chỗ (localize sai)",
    "Khác",
)
"""Lý do mẫu cho dropdown Reject trên dashboard (02-gates.md: 'dropdown lý do mẫu + ô text')."""


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    ticket_id: str
    decision: GateDecision


def pick_choice(
    *,
    approve: bool = False,
    reject: bool = False,
    reject_final: bool = False,
    choice: str | None = None,
) -> str:
    """Từ cờ CLI → đúng một lựa chọn. Sai → GateError."""
    picked = [
        c
        for c, on in (
            ("approve", approve),
            ("reject", reject),
            ("reject_final", reject_final),
            (choice, bool(choice)),
        )
        if on
    ]
    if len(picked) != 1 or picked[0] is None:
        raise GateError("chọn đúng một: --approve | --reject | --reject-final | --choice X")
    return str(picked[0])


def check_choice(
    gate: GateId,
    choice: str,
    note: str | None,
    *,
    options: Iterable[str] | None = None,
) -> None:
    """Lựa chọn hợp lệ với gate? Lý do có khi bắt buộc? options: id option thật của escalation (A, B…)."""
    allowed = ALLOWED.get(gate, frozenset())
    if choice not in allowed:
        raise GateError(
            f"gate {gate.value} không nhận lựa chọn {choice!r}",
            hint=f"hợp lệ: {', '.join(sorted(allowed))}",
        )
    if choice in ESCALATION_CHOICES:
        opts = set(options or ())
        if not opts:
            raise GateError(
                f"ticket này không có câu hỏi escalation, không chọn {choice!r} được",
                hint="dùng approve hoặc reject",
            )
        if choice != "other" and choice not in opts:
            raise GateError(
                f"escalation không có option {choice!r}", hint=f"option: {', '.join(sorted(opts))}"
            )
    if choice in NOTE_REQUIRED and not (note or "").strip():
        raise GateError(
            f"{choice} cần lý do (note) — người dạy hệ thống, không sửa code",
            hint="ddt resolve … --note 'lý do' · web: ô lý do bắt buộc",
        )


def resolve_gate(
    svc: GateService,
    og: OpenGate,
    *,
    choice: str,
    by: str,
    note: str | None,
    clock: Clock,
    options: Iterable[str] | None = None,
) -> GateDecision:
    """Hợp lệ hoá rồi ghi vào gate_requests. KHÔNG emit event — runner/worker emit gate_resolved khi poll thấy."""
    check_choice(og.ticket.gate, choice, note, options=options)
    decision = GateDecision(choice=choice, by=by, at=clock.now(), note=(note or None))
    svc.resolve(og.id, decision)
    return decision


def batch_approve(
    svc: GateService,
    tickets: Sequence[OpenGate],
    *,
    by: str,
    clock: Clock,
    tiers: Iterable[str] = ("T0",),
    note: str | None = None,
) -> tuple[list[ResolveOutcome], list[tuple[str, str]]]:
    """Approve nhiều ticket một lần — chỉ tier trong `tiers` (policy gates.g1.batch_approve_tiers).

    Trả (đã approve, [(ticket_id, lý do bỏ qua)]). Ticket đã resolved giữa chừng → bỏ qua, không lỗi.
    """
    allowed_tiers = set(tiers)
    done: list[ResolveOutcome] = []
    skipped: list[tuple[str, str]] = []
    for og in tickets:
        if og.tier not in allowed_tiers:
            skipped.append((og.id, f"tier {og.tier} không trong batch_approve_tiers"))
            continue
        if og.ticket.gate is not GateId.G1 and og.ticket.gate is not GateId.G0:
            skipped.append((og.id, f"gate {og.ticket.gate.value} không batch được"))
            continue
        try:
            d = resolve_gate(svc, og, choice="approve", by=by, note=note, clock=clock)
        except StoreError as e:
            skipped.append((og.id, e.message))
            continue
        done.append(ResolveOutcome(og.id, d))
    return done, skipped


__all__ = [
    "ALLOWED",
    "ESCALATION_CHOICES",
    "NOTE_REQUIRED",
    "REJECT_REASONS",
    "GateError",
    "ResolveOutcome",
    "batch_approve",
    "check_choice",
    "pick_choice",
    "resolve_gate",
]

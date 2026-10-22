"""G0_WAIT / G1_WAIT — mở ticket (idempotent), poll, áp default_on_timeout. Người quyết qua `ddt resolve`.

Autonomy `autonomous` (preset auto_decide): ticket vẫn mở để lưu dấu vết nhưng harness quyết NGAY — G0 đi tiếp
(không nâng tier), G1 theo bằng chứng (fail → rework, còn lại → merge), escalation lấy khuyến nghị agent, G2 cho phép
một lần. Không bao giờ chờ người: thi đấu, người chạm máy = −50 điểm.

facts trả: gate{choice, by, note, is_timeout, gate} · g0_visits · conservative (timeout G0)
Chưa có quyết định → facts[WAITING]=True → runner để status waiting_gate.
"""

from __future__ import annotations

import contextlib
import dataclasses
from typing import Any

from digital_development_team.config.models import GateCfg
from digital_development_team.core.enums import EventType, GateId, Stage, Tier
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import GateDecision, GateRequest, GateTicket, StageResult
from digital_development_team.policy.approvals import ApprovalStore
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.spec_amend import apply_chosen_patch
from digital_development_team.runner.stages.base import WAITING, result
from digital_development_team.runner.stages.escalation import (
    G2_CHOICES,
    answer_entry,
    read_escalation,
    resolve,
    resolve_g2,
    write_resolved,
)
from digital_development_team.runner.state import IssueState

_REOPEN = "_reopen"
AUTO_ACTOR = "harness:autonomous"


def _positive(t: float | int | None) -> float | None:
    """0 hoặc âm = KHÔNG timeout (chờ người). 0 từng bị hiểu là 'hết hạn ngay' → T0/T1 tự merge không ai duyệt."""
    return None if t is None or float(t) <= 0 else float(t)


def _timeout_for(cfg_gate: GateCfg, gate: GateId, tier: Tier, ctx: StageContext) -> float | None:
    p = ctx.config.policy
    preset = p.autonomy_presets.get(p.autonomy)
    if gate is GateId.G0 and tier is not Tier.T3 and preset is not None:
        return _positive(preset.g0_timeout)
    if gate is GateId.G1 and tier is Tier.T2 and preset is not None:
        return _positive(preset.g1_t2_timeout)
    t = cfg_gate.timeout_sec.get(tier.value) if cfg_gate.timeout_sec else None
    return _positive(t)


_NOT_ESCALATION: dict[str, Any] = {
    "escalation": False,
    "route": "",
    "return_to": "",
    "chosen_action": "",
    "recommendation": "",
    "override": False,
}
"""gate facts luôn đủ khoá → transitions so sánh gate.route/return_to không bao giờ thiếu giá trị."""


def last_g2_denial(ctx: StageContext, issue: str) -> dict[str, Any] | None:
    """Lời gọi gần nhất bị chặn vì cần G2 (dispatcher ghi gate + approval_key vào tool_denied)."""
    from digital_development_team.eventlog.reader import read_events

    for e in reversed(read_events(ctx.svc.layout, issue)):
        if e.event is EventType.TOOL_DENIED and e.data.get("gate") == "G2" and e.data.get("approval_key"):
            return {
                "tool": e.tool.name if e.tool else None,
                "target": e.data.get("path") or e.data.get("cmd"),
                "reason": e.tool.denied_reason if e.tool else None,
                "approval_key": e.data["approval_key"],
                "by": e.actor,
                "seq": e.seq,
            }
    return None


def og_summary(ctx: StageContext, ticket: GateTicket) -> dict[str, Any] | None:
    og = ctx.gates.get(ticket.id)
    return dict(og.summary) if og is not None and isinstance(og.summary, dict) else None


def _pending(state: IssueState) -> dict[str, Any] | None:
    esc = state.facts.get("escalated")
    return dict(esc) if state.facts.get("escalation_pending") and isinstance(esc, dict) else None


class GateWaitStage:
    uses_llm = False

    def __init__(self, stage: Stage, gate: GateId) -> None:
        self.stage = stage
        self.gate = gate

    def _cfg(self, ctx: StageContext) -> GateCfg:
        g = ctx.config.policy.gates
        return {GateId.G0: g.g0, GateId.G1: g.g1, GateId.G2: g.g2, GateId.G3: g.g3}[self.gate]

    def _summary(self, state: IssueState, ctx: StageContext) -> dict[str, Any]:
        store = ctx.svc.store
        out: dict[str, Any] = {"stage": state.stage.value, "tier": state.tier.value, "rework": state.rework}
        for name, slot, keys in (
            ("intake", "intake.yaml", ("type", "severity", "risk_tier", "confidence", "rationale")),
            ("decision", "decision.json", ("action", "confidence", "rationale", "summary_for_human")),
            ("spec", "spec.yaml", ("goal", "ambiguities")),
        ):
            try:
                content = store.read(state.issue_id, slot).content
            except StoreError:
                continue
            out[name] = {k: content[k] for k in keys if k in content}
        pending = _pending(state)
        if pending is not None:
            keep = ("raised_by", "stage", "type", "question", "recommendation", "count")
            out["escalation"] = {k: pending[k] for k in keep if k in pending}
        return out

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        cfg = self._cfg(ctx)
        auto = ctx.config.policy.auto_decide
        attempt = int(state.facts.get(f"{self.gate.value}_attempts", 0)) + 1
        ticket: GateTicket
        escalation_uri: str | None = None
        if state.pending_gate:
            og = ctx.gates.get(state.pending_gate)
            if og is None:
                raise StoreError(
                    f"gate ticket {state.pending_gate} mất khỏi DB",
                    hint="ddt debug reindex không phục hồi gate",
                )
            ticket = og.ticket
            escalation_uri = og.escalation_uri
        else:
            # chỉ gắn form escalation khi có câu hỏi đang chờ — form cũ đã trả lời không kéo sang ticket khác
            pending = _pending(state)
            escalation = None
            if pending is not None:
                with contextlib.suppress(StoreError):
                    escalation = ctx.svc.store.latest(state.issue_id, "escalation.yaml")
            timeout = _timeout_for(cfg, self.gate, state.tier, ctx)
            gate = self.gate
            summary = self._summary(state, ctx)
            if pending is not None and pending.get("destructive"):
                timeout = None  # hành động phá huỷ: không có mặc định, chờ người
                g2 = last_g2_denial(ctx, state.issue_id)
                if g2 is not None:  # đúng lời gọi bị chặn → ticket G2 duyệt từng lời gọi
                    gate = GateId.G2
                    summary["g2"] = g2
            ticket_escalation = escalation.uri if escalation else None
            ticket = ctx.gates.open(
                GateRequest(
                    issue_id=state.issue_id,
                    gate=gate,
                    attempt=attempt,
                    tier=state.tier,
                    summary={**summary, "auto_decided": True} if auto else summary,
                    escalation_uri=ticket_escalation,
                    timeout_s=timeout,
                )
            )
            state.pending_gate = ticket.id
            state.facts[f"{self.gate.value}_attempts"] = attempt
            if not auto:
                return result(
                    self.stage,
                    **{WAITING: True},
                    gate_opened={"ticket": ticket.id, "timeout_s": ticket.timeout_s},
                )
            escalation_uri = ticket_escalation

        decision = ctx.gates.poll(ticket, now=ctx.clock.now())
        if decision is None and auto:
            decision = GateDecision(choice="timeout", by=AUTO_ACTOR, at=ctx.clock.now(), is_timeout=True)
            ctx.gates.resolve(ticket.id, decision)
        if decision is None:
            return result(self.stage, **{WAITING: True})

        auto_now = auto and decision.by == AUTO_ACTOR
        if escalation_uri and _pending(state) is not None:
            facts = self._apply_escalation(decision, cfg, state, ticket, ctx, auto=auto_now)
        else:
            facts = self._apply(decision, cfg, state, ticket, auto=auto_now)
        reopen = facts.pop(_REOPEN, None)
        if reopen:
            # hết giờ nhưng sửa code không giải quyết được (kiểm chứng không kết luận, Leader muốn hỏi người…):
            # trả về Dev chỉ đốt rework rồi BLOCKED → mở lại ticket, KHÔNG timeout, chờ người.
            nxt = ctx.gates.open(
                GateRequest(
                    issue_id=state.issue_id,
                    gate=ticket.gate,
                    attempt=attempt,
                    tier=state.tier,
                    summary={**self._summary(state, ctx), "reopened_after_timeout": reopen},
                    timeout_s=None,
                )
            )
            state.pending_gate = nxt.id
            state.facts[f"{self.gate.value}_attempts"] = attempt
            return result(
                self.stage,
                **{WAITING: True},
                gate_opened={"ticket": nxt.id, "timeout_s": None},
                gate_reopened={"after": ticket.id, "reason": reopen},
            )
        state.pending_gate = None
        if self.gate is GateId.G0:
            facts["g0_visits"] = state.g0_visits() + 1
        return result(self.stage, **facts)

    def _apply(
        self, d: GateDecision, cfg: GateCfg, state: IssueState, ticket: GateTicket, *, auto: bool = False
    ) -> dict[str, Any]:
        choice = d.choice
        facts: dict[str, Any] = {}
        if auto:
            choice, note = self._auto_choice(state)
            d = d.model_copy(update={"note": note})
        elif d.is_timeout:
            mode = cfg.default_on_timeout or "block"
            if mode == "proceed_conservative":
                choice = "approve"
                facts["conservative"] = True
                state.bump_tier(state.tier.bump(1))
            elif mode == "proceed_with_recommendation":
                # 02-gates: tự merge sau timeout CHỈ KHI Verifier + Security đều pass
                rec = state.facts.get("decision") or {}
                sec = (state.facts.get("sec_report") or {}).get("verdict")
                verdict = (state.facts.get("evidence") or {}).get("verdict")
                ok = rec.get("action") == "merge" and verdict == "pass" and sec in (None, "pass")
                if ok:
                    choice = "approve"
                elif verdict == "fail" or sec == "fail" or rec.get("action") == "rework":
                    choice = "reject"  # Dev sửa được: ghi rõ lý do cho lần IMPLEMENT kế
                    note = d.note or (
                        f"G1 timed out and was auto-rejected: verifier={verdict or '?'}, "
                        f"security={sec or '?'}, leader={rec.get('action') or '?'}"
                    )
                    d = d.model_copy(update={"note": note})
                else:
                    return {
                        _REOPEN: f"timeout with verifier={verdict or '?'}, leader={rec.get('action') or '?'}"
                    }
            elif mode == "skip_issue":
                choice = "reject"
            else:
                choice = "reject"
        wait_sec = max(0, int((d.at - ticket.opened_at).total_seconds()))
        facts["gate"] = {
            "gate": ticket.gate.value,
            "choice": choice,
            "by": d.by,
            "note": d.note,
            "is_timeout": d.is_timeout,
            "wait_sec": wait_sec,
            **_NOT_ESCALATION,
        }
        return facts

    def _auto_choice(self, state: IssueState) -> tuple[str, str]:
        """Harness tự quyết (autonomous). G0: đi tiếp, không nâng tier. G1: bằng chứng fail / Leader muốn rework →
        reject (Dev sửa, giới hạn bởi max_rework); còn lại (kể cả inconclusive) → approve: nộp bài vẫn hơn 0 điểm."""
        if self.gate is not GateId.G1:
            return "approve", "auto-approved (autonomous): proceed without human"
        rec = state.facts.get("decision") or {}
        sec = (state.facts.get("sec_report") or {}).get("verdict")
        verdict = (state.facts.get("evidence") or {}).get("verdict")
        if verdict == "fail" or sec == "fail" or rec.get("action") == "rework":
            return "reject", (
                f"auto-rejected (autonomous): verifier={verdict or '?'}, security={sec or '?'}, "
                f"leader={rec.get('action') or '?'}"
            )
        return (
            "approve",
            f"auto-approved (autonomous): verifier={verdict or '?'}, leader={rec.get('action') or '?'}",
        )

    def _apply_escalation(
        self,
        d: GateDecision,
        cfg: GateCfg,
        state: IssueState,
        ticket: GateTicket,
        ctx: StageContext,
        *,
        auto: bool = False,
    ) -> dict[str, Any]:
        """Quyết định trên card escalation → hành động. Lưu câu trả lời vào escalation.yaml và facts.human_answers."""
        issue = state.issue_id
        esc = read_escalation(ctx.svc.store, issue) or {}
        pending = _pending(state) or {}
        stage = str(pending.get("stage") or esc.get("stage") or "")
        choice = d.choice
        facts: dict[str, Any] = {}
        if auto:  # khuyến nghị của agent; G2 cho phép đúng một lời gọi (luật chặn cứng vẫn áp ở dispatcher)
            choice = "allow_once" if ticket.gate is GateId.G2 else "approve"
        elif d.is_timeout:
            mode = str(esc.get("default_on_timeout") or cfg.default_on_timeout or "block")
            if mode in ("proceed_with_recommendation", "proceed_conservative"):
                choice = "approve"  # = chọn khuyến nghị
                if mode == "proceed_conservative":
                    facts["conservative"] = True
                    state.bump_tier(state.tier.bump(1))
            else:
                choice = "reject"
        g2 = (og_summary(ctx, ticket) or {}).get("g2") if ticket.gate is GateId.G2 else None
        if g2 and choice in G2_CHOICES:
            res = resolve_g2(esc, choice, d.note, stage=stage, g2=g2)
            if choice != "deny":
                ApprovalStore(ctx.svc.layout).grant(
                    issue,
                    key=str(g2["approval_key"]),
                    scope="once" if choice == "allow_once" else "issue",
                    by=d.by,
                    at=d.at,
                    detail=str(g2.get("target") or g2.get("tool") or ""),
                )
        else:
            res = resolve(esc, choice, d.note, stage=stage)
            if res.route != "abort":
                amended = apply_chosen_patch(
                    state, ctx, esc, res.choice, by=d.by, stage=Stage(stage) if stage in Stage else None
                )
                if amended is not None:
                    facts["spec_amended"] = {
                        "ok": amended.ok,
                        "added": amended.added,
                        "problems": amended.problems,
                    }
                    if not amended.ok:  # người chọn nhưng patch không an toàn → agent biết vì sao không đổi
                        extra = "spec_patch NOT applied: " + "; ".join(amended.problems)
                        res = dataclasses.replace(res, chosen_action=f"{res.chosen_action} ({extra})"[:400])
        wait_sec = max(0, int((d.at - ticket.opened_at).total_seconds()))
        write_resolved(ctx.svc.store, issue, esc, res, by=d.by, at=d.at, note=d.note, is_timeout=d.is_timeout)
        answers = list(state.facts.get("human_answers") or [])
        answers.append(answer_entry(esc, res, by=d.by, note=d.note, wait_sec=wait_sec))
        facts.update(
            {
                "gate": {
                    "gate": ticket.gate.value,
                    "choice": res.choice,
                    "by": d.by,
                    "note": d.note,
                    "is_timeout": d.is_timeout,
                    "wait_sec": wait_sec,
                    "escalation": True,
                    "route": res.route,
                    "return_to": res.return_to,
                    "chosen_action": res.chosen_action,
                    "recommendation": res.recommendation,
                    "override": res.override and not d.is_timeout,
                },
                "human_answers": answers,
                "escalation_pending": False,
            }
        )
        return facts

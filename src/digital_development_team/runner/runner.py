"""Runner — vòng lặp chính (10-harness/01-runner.md; sync + thread).

Mỗi vòng:  STOP? → config snapshot → [phase ready] stage.run → merge facts → checkpoint(stage_done)
           → [phase stage_done] transitions.next(scope) → counters → checkpoint(ready) → emit.
Hai checkpoint mỗi vòng: crash sau stage xong → resume chỉ tính transition, không chạy lại stage.
Runner là process DUY NHẤT ghi events.jsonl của issue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from digital_development_team.budget.meter import BudgetMeter, IssueMeter
from digital_development_team.budget.resolve import (
    apply_transport_overhead,
    issue_budget,
    resolve_stage_budget,
)
from digital_development_team.config.loader import ResolvedConfig
from digital_development_team.config.models import IssueBudget
from digital_development_team.core.context import Actor, CancelToken
from digital_development_team.core.enums import EventType, IssueStatus, Stage
from digital_development_team.core.errors import (
    BudgetExceeded,
    Cancelled,
    DDTError,
    MatchDeferred,
    ProviderPaused,
)
from digital_development_team.core.events import BudgetSnapshot, EventDraft, HumanInfo
from digital_development_team.core.models import StageResult
from digital_development_team.eventlog.reader import read_events
from digital_development_team.gates.service import GateService
from digital_development_team.gates.stop import StopSwitch
from digital_development_team.ingest.queue import Queue
from digital_development_team.risk.tier import compute_tier
from digital_development_team.runner.context import Services, StageContext
from digital_development_team.runner.feedback import FACT as RETRY_FACT
from digital_development_team.runner.feedback import build_retry_feedback
from digital_development_team.runner.recovery import (
    build_blocked_report,
    category_of,
    choose,
    classify_failure,
    recovery_note,
)
from digital_development_team.runner.scope import LazyScope
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.base import WAITING
from digital_development_team.runner.stages.parallel import ParallelStages
from digital_development_team.runner.state import IssueState
from digital_development_team.runner.transitions import Decision, TransitionTable

log = logging.getLogger(__name__)

Status = Literal["done", "blocked", "paused", "waiting_gate", "running"]
EXIT: dict[str, int] = {"done": 0, "blocked": 1, "paused": 3, "waiting_gate": 4, "running": 0}
ACTOR = "runner"


@dataclass(frozen=True, slots=True)
class RunReport:
    issue_id: str
    status: Status
    stage: Stage
    reason: str | None = None
    loops: int = 0
    events: int = 0

    @property
    def exit_code(self) -> int:
        return EXIT[self.status]


@dataclass
class _Loop:
    cfg: ResolvedConfig
    loops: int = 0
    events: int = 0
    fingerprint: str = ""
    stage_started_mono: float = 0.0
    stage_meter: BudgetMeter | None = None
    waits_emitted: set[str] = field(default_factory=set)
    deadline_at: datetime | None = None  # item.json#deadline_at (đồng hồ server) — đọc một lần mỗi lượt run


class Runner:
    def __init__(
        self,
        svc: Services,
        *,
        handlers: StageRegistry,
        config_source: Callable[[], ResolvedConfig] | None = None,
        stop: StopSwitch | None = None,
        gates: GateService | None = None,
        stub: bool = False,
    ) -> None:
        self._svc = svc
        self._handlers = handlers
        self._config_source = config_source or (lambda: svc.config)
        self._stop = stop or StopSwitch(svc.layout)
        self._gates = gates or GateService(svc.db, svc.clock)
        self._stub = stub
        self._queue = Queue(svc.db, svc.clock)
        self._table: TransitionTable | None = None
        self._table_fp = ""

    # ───── public ─────
    @property
    def services(self) -> Services:
        return self._svc

    def load_state(self, issue_id: str) -> IssueState:
        cp = self._svc.checkpoints.load(issue_id)
        return IssueState.from_checkpoint(cp) if cp else IssueState.new(issue_id)

    def run(
        self,
        issue_id: str,
        *,
        cancel: CancelToken,
        until: Stage | None = None,
        wait_gates: bool = False,
        poll_s: float = 1.0,
    ) -> RunReport:
        state = self.load_state(issue_id)
        lp = _Loop(
            cfg=self._config_source(), stage_started_mono=self._svc.clock.monotonic()
        )  # resume: duration tính từ lúc chạy lại
        lp.fingerprint = lp.cfg.fingerprint
        lp.deadline_at = _item_deadline(self._svc, issue_id)
        resumed = self._svc.checkpoints.exists(issue_id)
        if state.terminal:
            return RunReport(
                issue_id,
                "done" if state.status is IssueStatus.DONE else "blocked",
                state.stage,
                "đã kết thúc",
            )
        if resumed:
            state.status = IssueStatus.RUNNING
            self._emit(state, lp, EventType.RESUMED, {"stage": state.stage.value, "phase": state.phase})
        self._queue_status(issue_id, "running")
        self._canary_fact(state)

        while True:
            # G3
            if self._stop.is_stopped():
                return self._pause(state, lp, f"STOP ({self._stop.info()})")
            if cancel.is_cancelled():
                return self._pause(state, lp, cancel.reason or "cancelled")
            lp.cfg = self._config_source()
            if lp.cfg.fingerprint != lp.fingerprint:
                lp.fingerprint = lp.cfg.fingerprint
                self._emit(state, lp, EventType.CONFIG_RELOADED, {"fingerprint": lp.fingerprint[:12]})
            lp.loops += 1
            lp.deadline_at = _item_deadline(
                self._svc, issue_id
            )  # match.json đổi khi trận PAUSED (hạn lùi lại)
            left = self._deadline_left(lp)
            if left is not None and left <= 0 and state.stage not in (Stage.DONE, Stage.BLOCKED):
                # quá hạn item (trừ biên nộp bài): làm tiếp chỉ đốt credit — item CLOSED thì không nộp được nữa
                return self._block(
                    state,
                    lp,
                    f"DEADLINE: quá hạn item ở {state.stage.value}",
                    hint="item đã/sắp CLOSED — bỏ để dành thời gian cho item khác",
                    failure_class="deadline",
                )

            try:
                if state.phase == "ready":
                    outcome = self._run_stage(state, lp, cancel)
                    if outcome == "waiting":
                        if not wait_gates:
                            return RunReport(
                                issue_id, "waiting_gate", state.stage, state.pending_gate, lp.loops, lp.events
                            )
                        if cancel.wait(poll_s):
                            return self._pause(state, lp, "cancelled khi chờ gate")
                        continue
                    if until is not None and state.stage == until:
                        return RunReport(
                            issue_id, "running", state.stage, f"--until {until.value}", lp.loops, lp.events
                        )
                    continue
                # phase stage_done → transition
                report = self._transition(state, lp)
                if report is not None:
                    return report
            except Cancelled as e:
                if not cancel.is_cancelled() and e.message == "deadline":
                    # hết wall-clock của stage (token con), không phải người bấm STOP hay Ctrl-C. Pause ở đây làm
                    # issue lặp vô hạn (mỗi lần chạy lại có hạn mới) → xử lý như circuit breaker của budget.
                    self._emit(
                        state,
                        lp,
                        EventType.CIRCUIT_BREAKER,
                        {"scope": "stage", "axis": "wall", "stage": state.stage.value},
                    )
                    reason = f"BUDGET: hết thời gian stage {state.stage.value}"
                    if self._recover(state, lp, state.stage, reason):
                        continue
                    return self._block(
                        state, lp, reason, hint="Tăng wall trong budgets.yaml hoặc tách issue nhỏ hơn"
                    )
                return self._pause(state, lp, str(e))
            except (ProviderPaused, MatchDeferred) as e:
                return self._pause(state, lp, f"{e.code}: {e.message}")
            except BudgetExceeded as e:
                reason = f"{e.code}: {e.message}"
                if self._recover(state, lp, state.stage, reason):
                    continue
                return self._block(state, lp, reason, hint=e.hint)
            except DDTError as e:  # lỗi harness/config: không phải lỗi agent → không chạy lại mù
                return self._block(
                    state, lp, f"{e.code}: {e.message}", hint=e.hint, failure_class="environment"
                )

    # ───── stage ─────
    def _handler_for(self, state: IssueState) -> Any:
        group = state.parallel
        if group:
            return ParallelStages([self._handlers.get(s) for s in group])
        return self._handlers.get(state.stage)

    def _run_stage(self, state: IssueState, lp: _Loop, cancel: CancelToken) -> Literal["done", "waiting"]:
        handler = self._handler_for(state)
        entered_key = f"{state.stage.value}:{state.attempt}:{state.rework}:{state.reloc}:{state.mg_retries}"
        if state.facts.get("_stage_entered") != entered_key:
            enter: dict[str, Any] = {
                "tier": state.tier.value,
                "rework": state.rework,
                "attempt": state.attempt,
            }
            last = state.facts.pop("_last_transition", None)
            if last:
                enter["via"] = last
            self._emit(state, lp, EventType.STAGE_ENTER, enter)
            state.facts["_stage_entered"] = entered_key
            lp.stage_started_mono = self._svc.clock.monotonic()
        intake = state.facts.get("intake")
        is_cr = isinstance(intake, dict) and intake.get("type") == "change_request"
        task_type = intake.get("type") if isinstance(intake, dict) else None
        ref = resolve_stage_budget(
            lp.cfg.budgets,
            state.stage,
            state.tier,
            rework=state.rework,
            is_change_request=is_cr,
            multiplier=lp.cfg.policy.task_profile(task_type).budget_multiplier,
        )
        role = getattr(handler, "role", None)
        pick = getattr(handler, "model_role_for", None)
        if role and callable(pick):
            role = pick(state, lp.cfg)  # vd dev_rework: budget theo transport của model thật sẽ chạy
        if ref is not None and role and self._svc.models is not None:
            transport = self._svc.models.overhead_key_for(role)
            if transport != "api":
                ref = apply_transport_overhead(ref, lp.cfg.budgets, transport)
        meter = BudgetMeter(ref, self._svc.clock) if (ref and handler.uses_llm) else None
        dev_transport = self._svc.models.overhead_key_for("dev") if self._svc.models is not None else "api"
        issue_meter = IssueMeter(
            _scaled(
                issue_budget(lp.cfg.budgets, state.tier, transport=dev_transport), self._budget_pct(state, lp)
            ),
            self._svc.clock,
            usage=state.usage,
        )
        stage_timeout = ref.wall_s if ref else None
        left = self._deadline_left(lp)
        if left is not None:  # stage không được chạy quá hạn item (cả stage 0 LLM như MACHINE_GATE)
            stage_timeout = max(1.0, min(stage_timeout, left) if stage_timeout is not None else left)
        ctx = StageContext(
            svc=self._svc,
            config=lp.cfg,
            state=state,
            cancel=cancel.child(timeout_s=stage_timeout),
            clock=self._svc.clock,
            gates=self._gates,
            budget=meter,
            stub=self._stub,
        )
        result: StageResult = handler.run(state, ctx)
        usage = result.usage
        if usage.wall_s == 0 and state.stage not in (Stage.G0_WAIT, Stage.G1_WAIT):
            # wall-clock của stage (không tính thời gian chờ người) → checkpoint.usage.wall_s cộng dồn
            elapsed = max(0.0, self._svc.clock.monotonic() - lp.stage_started_mono)
            usage = usage.model_copy(update={"wall_s": elapsed})
        state.usage = state.usage + usage
        issue_meter.add(usage)
        try:
            issue_meter.check()
        except BudgetExceeded as e:
            self._emit(
                state,
                lp,
                EventType.CIRCUIT_BREAKER,
                {"scope": e.scope, "axis": e.axis, "stage": state.stage.value},
                budget=BudgetSnapshot(**issue_meter.snapshot()),
            )
            raise
        facts = dict(result.facts)
        waiting = bool(facts.pop(WAITING, False))
        state.merge_facts(facts)
        esc = facts.get("escalated")
        if isinstance(esc, dict) and (facts.get("outcome") or {}).get("kind") == "escalation":
            self._emit(
                state,
                lp,
                EventType.ESCALATION_RAISED,
                {k: esc.get(k) for k in ("gate", "type", "raised_by", "question", "recommendation", "count")},
                artifact_ref=(facts.get("outcome") or {}).get("uri"),
            )
        if waiting:
            state.status = IssueStatus.WAITING_GATE
            self._save(state)
            if state.pending_gate and state.pending_gate not in lp.waits_emitted:
                lp.waits_emitted.add(state.pending_gate)
                self._emit(
                    state,
                    lp,
                    EventType.GATE_WAIT,
                    {"ticket": state.pending_gate},
                    human=HumanInfo(who="?", gate=state.stage.value.split("_")[0]),
                )
            return "waiting"
        if "gate" in facts:
            g = facts["gate"]
            self._emit(
                state,
                lp,
                EventType.GATE_TIMEOUT if g.get("is_timeout") else EventType.GATE_RESOLVED,
                {"ticket": state.facts.get("gate_opened", {}).get("ticket")},
                human=HumanInfo(
                    who=str(g.get("by")),
                    gate=g.get("gate"),
                    choice=g.get("choice"),
                    note=g.get("note"),
                    wait_sec=g.get("wait_sec"),
                ),
            )
            if g.get("override"):
                # người chọn khác khuyến nghị của agent → Learner tính ×3 (04-escalation)
                self._emit(
                    state,
                    lp,
                    EventType.HUMAN_OVERRIDE,
                    {
                        "recommendation": g.get("recommendation"),
                        "choice": g.get("choice"),
                        "chosen_action": g.get("chosen_action"),
                        "return_to": g.get("return_to"),
                    },
                    human=HumanInfo(
                        who=str(g.get("by")), gate=g.get("gate"), choice=g.get("choice"), note=g.get("note")
                    ),
                )
        tier_data = self._apply_tier(state, lp)
        state.status = IssueStatus.RUNNING
        state.phase = "stage_done"
        self._save(state)
        duration_ms = int((self._svc.clock.monotonic() - lp.stage_started_mono) * 1000)
        self._emit(
            state,
            lp,
            EventType.STAGE_EXIT,
            {
                "duration_ms": duration_ms,
                "result": _summarize(facts),
                "tokens": result.usage.tokens,
                **tier_data,
            },
        )
        return "done"

    def _apply_tier(self, state: IssueState, lp: _Loop) -> dict[str, Any]:
        """MAX tier_rules trên scope hiện tại. Tier là TẠM (T1 placeholder) cho tới khi có rule thật khớp;
        từ đó không bao giờ hạ. Ghi lý do vào facts.tier_bumped_because."""
        scope = LazyScope(state, self._svc.store, lp.cfg)
        before = state.tier
        provisional = not state.facts.get("_tier_locked")
        res = compute_tier(lp.cfg.policy.tier_rules, scope, current=None if provisional else before)
        data: dict[str, Any] = {"tier": res.tier.value}
        changed = False
        if provisional:
            if res.reasons:
                state.facts["_tier_locked"] = True
            if res.tier is not before:
                state.tier = res.tier
                changed = True
        else:
            changed = state.bump_tier(res.tier)
        if changed:
            state.facts["tier_bumped_because"] = res.bumped_because
            data["tier_bumped"] = {
                "from": before.value,
                "to": res.tier.value,
                "because": res.bumped_because[:3],
            }
        return data

    # ───── transition ─────
    def _table_for(self, cfg: ResolvedConfig) -> TransitionTable:
        if self._table is None or self._table_fp != cfg.fingerprint:
            self._table = TransitionTable.from_policy(cfg.policy)
            self._table_fp = cfg.fingerprint
        return self._table

    def _transition(self, state: IssueState, lp: _Loop) -> RunReport | None:
        table = self._table_for(lp.cfg)
        scope = LazyScope(state, self._svc.store, lp.cfg)
        decision: Decision = table.next(state.stage, scope)
        rule = decision.rule
        data: dict[str, Any] = {"from": state.stage.value, **decision.describe()}
        if rule.rework:
            state.rework += 1
            self._emit(
                state, lp, EventType.REWORK, {"rework": state.rework, "reason": rule.note or rule.when}
            )
        if rule.reloc:
            state.reloc += 1
            self._emit(
                state, lp, EventType.RE_LOCALIZE, {"reloc": state.reloc, "reason": rule.note or rule.when}
            )
        if rule.mg_retries:
            state.mg_retries += 1
        if rule.bump:
            counters = dict(state.facts.get("counters") or {})
            for name in rule.bump:
                counters[name] = int(counters.get(name, 0)) + 1
            state.facts["counters"] = counters
        from_stage = state.stage
        targets = list(decision.to)
        if len(targets) > 1:
            # REVIEW có rule skip (T0) → bỏ khỏi nhóm song song
            kept = [
                t for t in targets if not (t is not targets[0] and table.has(t) and table.peek_skip(t, scope))
            ]
            if (
                Stage.REVIEW in targets and Stage.REVIEW not in kept
            ):  # Leader cần biết Security bỏ qua theo luật
                state.facts["review_skipped"] = (
                    f"tier {state.tier.value}, security checklist passed, no sensitive path"
                )
            else:
                state.facts.pop("review_skipped", None)
            targets = kept
        self._retry_feedback(state, from_stage, targets[0])
        state.facts.pop("outcomes", None)  # outcome từng nhánh VERIFY ∥ REVIEW chỉ dùng cho transition này
        state.set_parallel(tuple(targets))
        state.stage = targets[0]
        state.phase = "ready"
        state.attempt += 1 if targets[0] is Stage.IMPLEMENT and rule.rework else 0
        state.facts.pop("_stage_entered", None)
        if state.stage is Stage.DONE:
            state.status = IssueStatus.DONE
            self._save(state)
            self._emit(
                state,
                lp,
                EventType.ISSUE_DONE,
                {**data, "tokens": state.usage.tokens, "rework": state.rework},
            )
            self._queue_status(state.issue_id, "done")
            self._learn(state)
            return RunReport(state.issue_id, "done", state.stage, None, lp.loops, lp.events)
        if state.stage is Stage.BLOCKED:
            raw = state.facts.get("outcome")
            out: dict[str, Any] = raw if isinstance(raw, dict) else {}
            stage_reason = out.get("reason") if out.get("kind") == "blocked" else None  # lý do thật của stage
            reason = (
                rule.note
                or stage_reason
                or f"transition {data.get('from')} → BLOCKED (rule {decision.index})"
            )
            if not rule.final and self._recover(state, lp, from_stage, reason):
                return None
            return self._block(state, lp, reason, from_stage=from_stage, final=rule.final)
        state.status = IssueStatus.RUNNING
        state.facts["_last_transition"] = data
        self._save(state)
        log.debug("%s: %s → %s (rule %s)", state.issue_id, data["from"], data["to"], decision.index)
        return None

    def _retry_feedback(self, state: IssueState, from_stage: Stage, to_stage: Stage) -> None:
        """Quay lại IMPLEMENT/SPEC/LOCALIZE → dựng lý do lần trước bị trả về (0 LLM). Khối cũ bỏ khi stage nhận nó
        đã chạy xong (trừ khi nó chỉ dừng để hỏi người: câu trả lời quay về đúng stage đó)."""
        svc = self._svc

        def read_slot(slot: str) -> dict[str, Any] | None:
            try:
                return dict(svc.store.read(state.issue_id, slot).content)
            except DDTError:
                return None

        def read_artifact(ref: str) -> str | None:
            return svc.artifacts.read(ref) if svc.artifacts is not None else None

        fb = build_retry_feedback(
            from_stage=from_stage,
            to_stage=to_stage,
            facts=state.facts,
            read_slot=read_slot,
            read_artifact=read_artifact,
        )
        old = state.facts.get(RETRY_FACT)
        if fb is not None:
            state.facts[RETRY_FACT] = fb
        elif (
            isinstance(old, dict)
            and old.get("to") == from_stage.value
            and to_stage not in (Stage.G0_WAIT, Stage.G1_WAIT)
        ):
            state.facts.pop(RETRY_FACT, None)

    # ───── kết thúc ─────
    def _pause(self, state: IssueState, lp: _Loop, reason: str) -> RunReport:
        state.status = IssueStatus.PAUSED
        self._save(state)
        self._emit(
            state,
            lp,
            EventType.STOP_REQUESTED,
            {"reason": reason, "stage": state.stage.value, "phase": state.phase},
        )
        return RunReport(state.issue_id, "paused", state.stage, reason, lp.loops, lp.events)

    def _block(
        self,
        state: IssueState,
        lp: _Loop,
        reason: str,
        *,
        hint: str | None = None,
        from_stage: Stage | None = None,
        final: bool = False,
        failure_class: str | None = None,
    ) -> RunReport:
        from_stage = from_stage or state.stage
        if failure_class is None:
            failure_class = "human_abort" if final else self._classify(state, from_stage, reason)
        report_ref = self._write_blocked_report(state, lp, from_stage, reason, hint, failure_class)
        state.stage = Stage.BLOCKED
        state.phase = "ready"
        state.status = IssueStatus.BLOCKED
        state.set_parallel(())
        self._save(state)
        self._emit(
            state,
            lp,
            EventType.BLOCKED,
            {
                "reason": reason,
                "from": from_stage.value,
                "failure_class": failure_class,
                "category": category_of(failure_class),
                **({"hint": hint} if hint else {}),
            },
            artifact_ref=report_ref,
        )
        self._queue_status(state.issue_id, "blocked")
        self._learn(state)
        return RunReport(state.issue_id, "blocked", Stage.BLOCKED, reason, lp.loops, lp.events)

    # ───── phục hồi (runner/recovery.py) ─────
    def _read_slot(self, issue: str, slot: str) -> dict[str, Any] | None:
        try:
            return dict(self._svc.store.read(issue, slot).content)
        except DDTError:
            return None

    def _denials(self, issue: str) -> list[str]:
        out: list[str] = []
        for e in read_events(self._svc.layout, issue)[-40:]:
            if e.event is EventType.TOOL_DENIED and e.tool is not None and e.tool.denied_reason:
                out.append(str(e.tool.denied_reason))
        return out

    def _classify(self, state: IssueState, from_stage: Stage, reason: str) -> str:
        return classify_failure(
            from_stage=from_stage,
            reason=reason,
            facts=state.facts,
            read_slot=lambda slot: self._read_slot(state.issue_id, slot),
            denials=self._denials(state.issue_id),
        )

    def _deadline_left(self, lp: _Loop) -> float | None:
        """Giây còn lại tới hạn item trừ policy.deadline_margin_s (để kịp push + submit). None = item không có hạn."""
        if lp.deadline_at is None:
            return None
        left = (lp.deadline_at - self._svc.clock.now()).total_seconds()
        return left - lp.cfg.policy.deadline_margin_s

    def _budget_pct(self, state: IssueState, lp: _Loop) -> int:
        """Trần issue thêm %: đang phục hồi (recovery.extra_budget_pct) + người cấp khi reopen."""
        rec = state.facts.get("recovery")
        active = isinstance(rec, dict) and bool(rec.get("active"))
        grant = int(state.facts.get("budget_grant_pct", 0) or 0)
        return (lp.cfg.policy.recovery.extra_budget_pct if active else 0) + grant

    def _recover(self, state: IssueState, lp: _Loop, from_stage: Stage, reason: str) -> bool:
        """Sắp BLOCKED → bậc phục hồi đầu còn lượt khớp lớp lỗi. True = issue đã được đưa về stage đích."""
        cfg = lp.cfg.policy.recovery
        cls = self._classify(state, from_stage, reason)
        rec: dict[str, Any] = dict(state.facts.get("recovery") or {})
        counts = {str(k): int(v) for k, v in (rec.get("counts") or {}).items()}
        plan = choose(
            cfg,
            failure_class=cls,
            from_stage=from_stage,
            counts=counts,
            model_roles=list(lp.cfg.models.roles),
            split_enabled=lp.cfg.policy.split_enabled,
        )
        if plan is None:
            return False
        counts[plan.rung] = counts.get(plan.rung, 0) + 1
        step = {"rung": plan.rung, "class": cls, "from": from_stage.value, "to": plan.to.value}
        history = [*list(rec.get("history") or []), {**step, "reason": reason[:200]}]
        rec.update({"counts": counts, "history": history[-10:], "active": True})
        if plan.model_role:
            rec.update({"model_role": plan.model_role, "model_stage": plan.to.value})
        if plan.mode:
            rec["mode"] = plan.mode
        state.facts["recovery"] = rec
        fb = build_retry_feedback(
            from_stage=from_stage,
            to_stage=plan.to,
            facts=state.facts,
            read_slot=lambda slot: self._read_slot(state.issue_id, slot),
            read_artifact=lambda ref: self._svc.artifacts.read(ref) if self._svc.artifacts else None,
        )
        note = recovery_note(plan, reason)
        state.facts[RETRY_FACT] = {
            "from": from_stage.value,
            "to": plan.to.value,
            "text": (fb["text"] + "\n" + note) if fb else note,
            "refs": list(fb["refs"]) if fb else [],
        }
        state.stage = plan.to
        state.phase = "ready"
        state.status = IssueStatus.RUNNING
        state.pending_gate = None
        state.set_parallel(())
        state.facts.pop("_stage_entered", None)
        state.facts["_last_transition"] = {
            "from": from_stage.value,
            "to": plan.to.value,
            "recovery": plan.rung,
        }
        self._save(state)
        self._emit(
            state,
            lp,
            EventType.RECOVERY,
            {
                **step,
                "failure_class": cls,
                **({"model_role": plan.model_role} if plan.model_role else {}),
                "reason": reason[:200],
            },
        )
        return True

    def _write_blocked_report(
        self, state: IssueState, lp: _Loop, from_stage: Stage, reason: str, hint: str | None, cls: str
    ) -> str | None:
        svc = self._svc
        files: list[str] = []
        if state.worktree and svc.worktrees is not None:
            try:
                git = svc.worktrees.git
                wt = Path(state.worktree)
                base = state.base_commit or svc.worktrees.default_branch()
                changed = {p for _, p in git.status_porcelain(cwd=wt)} | set(
                    git.changed_files(cwd=wt, base=base)
                )
                files = sorted(changed)
            except DDTError:
                files = []
        spec_ref = svc.store.latest(state.issue_id, "spec.yaml")
        report = build_blocked_report(
            issue=state.issue_id,
            failure_class=cls,
            from_stage=from_stage,
            reason=reason,
            hint=hint,
            facts=state.facts,
            rework=state.rework,
            reloc=state.reloc,
            branch=state.branch,
            worktree=svc.layout.rel(Path(state.worktree)) if state.worktree else None,
            files_touched=files,
            spec_version=spec_ref.version if spec_ref else None,
            reopens=int(state.facts.get("reopens", 0) or 0),
            max_reopens=lp.cfg.policy.recovery.max_reopens,
            at=svc.clock.now().isoformat(),
            progress=self._progress(state.issue_id),
        )
        try:
            ref = svc.store.write(state.issue_id, "blocked_report.json", report, actor=Actor(role="runner"))
        except DDTError as e:  # báo cáo hỏng không được chặn việc dừng issue
            log.warning("%s: ghi blocked_report lỗi: %s", state.issue_id, e)
            return None
        return ref.uri

    def _progress(self, issue: str) -> dict[str, Any] | None:
        try:
            return dict(self._svc.store.read(issue, "progress.yaml").content)
        except DDTError:
            return None

    def _canary_fact(self, state: IssueState) -> None:
        """Tỷ lệ Verifier bắt canary (≥ MIN_SHOWN lần) → facts.verifier_canary_catch_rate cho tier_rules (+1 khi < 0.7)."""
        from digital_development_team.memory.canary_stats import catch_rate

        try:
            rate, _ = catch_rate(self._svc.db)
        except Exception:  # bảng chưa có (DB cũ chưa migrate) → bỏ qua, không chặn pipeline
            return
        if rate is None:
            state.facts.pop("verifier_canary_catch_rate", None)
        else:
            state.facts["verifier_canary_catch_rate"] = rate

    def _learn(self, state: IssueState) -> None:
        """Issue kết thúc → episode, kết quả từng role (kèm phiên bản JD), worker_stats, dòng standup; mỗi 10 issue
        thêm reports/perf_N.md. Tất định, 0 LLM. Lỗi ở đây chỉ ghi log, không bao giờ làm hỏng pipeline."""
        from digital_development_team.memory.outcomes import record_issue_outcome
        from digital_development_team.reporting.perf import maybe_write_perf
        from digital_development_team.reporting.retro import maybe_write_retro

        svc = self._svc
        issue = state.issue_id

        def read_slot(slot: str) -> dict[str, Any]:
            try:
                return dict(svc.store.read(issue, slot).content)
            except DDTError:
                return {}

        try:
            versions = {spec.name: spec.version for spec in svc.agents} if svc.agents is not None else {}
            now = svc.clock.now()
            cp = state.to_checkpoint(saved_at=now)
            events = read_events(svc.layout, issue)
            res = record_issue_outcome(
                db=svc.db,
                layout=svc.layout,
                checkpoint=cp,
                events=events,
                read_slot=read_slot,
                agent_versions=versions,
                now=now,
                no_learn=svc.config.policy.no_learn,
            )
            if res is not None:
                maybe_write_perf(svc.layout, svc.db, res.seq_no, now=now)
                maybe_write_retro(svc.layout, svc.db, res.seq_no, now=now)
                if not svc.config.policy.no_learn:
                    self._learn_followup(state, cp, events, res)
        except Exception as e:  # số liệu học hỏi không được chặn merge
            log.warning("%s: ghi kết quả học hỏi lỗi: %s", issue, e)

    def _learn_followup(self, state: IssueState, cp: Any, events: Any, res: Any) -> None:
        """Chấm fact/skill học được issue này đã dùng (tự nâng/gỡ) và xếp job cho Learner nếu issue đáng học."""
        from digital_development_team.memory.facts import FactStore
        from digital_development_team.memory.learned import LearnedSkills
        from digital_development_team.runner.learn import (
            apply_usage,
            enqueue,
            learn_reasons,
            same_type_passes,
        )

        svc = self._svc
        mcfg = svc.config.policy.memory
        changes = apply_usage(
            skills=LearnedSkills(svc.db, svc.layout, mcfg, clock=svc.clock),
            facts=FactStore(svc.layout, clock=svc.clock),
            cfg=mcfg,
            checkpoint=cp,
            result=res,
            events=events,
        )
        for ch in changes:
            promoted = "→ active" in ch
            svc.sink.emit(
                EventDraft(
                    issue_id=state.issue_id,
                    actor=ACTOR,
                    event=EventType.MEMORY_PROMOTE if promoted else EventType.MEMORY_DEMOTE,
                    data={"change": ch},
                )
            )
        intake = cp.facts.get("intake") if isinstance(cp.facts.get("intake"), dict) else {}
        passes = same_type_passes(svc, str(intake.get("affected_area") or ""), str(intake.get("type") or ""))
        reasons = learn_reasons(checkpoint=cp, events=events, result=res, cfg=mcfg, same_passes=passes)
        if enqueue(svc, state.issue_id, reasons):
            log.info("%s: xếp Learner (%s)", state.issue_id, ", ".join(reasons))

    # ───── helper ─────
    def _queue_status(self, issue_id: str, status: str) -> None:
        if self._queue.get(issue_id) is not None:
            self._queue.set_status(issue_id, status)

    def _save(self, state: IssueState) -> None:
        title = (
            (state.facts.get("item") or {}).get("title")
            if isinstance(state.facts.get("item"), dict)
            else None
        )
        self._svc.checkpoints.save(state.to_checkpoint(saved_at=self._svc.clock.now()), title=title)

    def _emit(self, state: IssueState, lp: _Loop, event: EventType, data: dict[str, Any], **kw: Any) -> None:
        ev = self._svc.sink.emit(
            EventDraft(issue_id=state.issue_id, actor=ACTOR, event=event, stage=state.stage, data=data, **kw)
        )
        state.last_seq = ev.seq
        lp.events += 1


def _item_deadline(svc: Services, issue_id: str) -> datetime | None:
    """Hạn item: match.json#deadline_at (bản mới nhất — Game Master dời hạn khi trận PAUSED) rồi tới
    item.json#deadline_at. ISO có múi giờ → datetime. Thiếu/hỏng → None (không chặn theo hạn)."""
    raw = None
    for slot in ("match.json", "item.json"):
        try:
            raw = svc.store.read(issue_id, slot).content.get("deadline_at")
        except DDTError:
            continue
        if raw:
            break
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        log.warning("%s: deadline_at hỏng %r — bỏ qua", issue_id, raw)
        return None
    return at if at.tzinfo is not None else None


def _scaled(ib: IssueBudget, pct: int) -> IssueBudget:
    if pct <= 0:
        return ib
    return IssueBudget(tokens=int(ib.tokens * (100 + pct) / 100), wall=round(ib.wall * (100 + pct) / 100, 2))


def _summarize(facts: dict[str, Any]) -> dict[str, Any]:
    """Tóm tắt facts cho stage_exit.data.result — không đổ cả artifact."""
    out: dict[str, Any] = {}
    for k, v in facts.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            out[k] = {
                kk: vv for kk, vv in v.items() if isinstance(vv, (str, int, float, bool)) and kk in _KEYS
            }
        elif isinstance(v, (str, int, float, bool)):
            out[k] = v
    return out


_KEYS = {
    "type",
    "risk_tier",
    "route",
    "confidence",
    "verdict",
    "action",
    "requires_human",
    "any_fail",
    "conflict",
    "choice",
    "kind",
    "commit",
    "title",
    "files",
    "steps",
    "ran",
}

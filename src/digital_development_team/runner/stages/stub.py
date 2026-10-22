"""Stub handlers — mọi stage agent/máy, 0 LLM, artifact hợp lệ. Cho `ddt run --stub-stages`, demo, test state machine.

overrides = {"intake": {"risk_tier": "T3"}, "evidence": {"verdict": "fail"}, "machine_gate": {"fail_times": 1}, …}
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from digital_development_team.core.context import Actor
from digital_development_team.core.enums import Stage
from digital_development_team.core.models import BudgetUsage, StageResult
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages import stub_data as sd
from digital_development_team.runner.stages.answer import AnswerStage
from digital_development_team.runner.stages.base import StageHandler, result
from digital_development_team.runner.stages.escalation import on_escalation
from digital_development_team.runner.state import IssueState

Builder = Callable[[IssueState, StageContext, dict[str, Any]], dict[str, Any]]
_STUB_USAGE = BudgetUsage(turns=1, llm_calls=1, tokens_in=1200, tokens_out=300)


class StubAgentStage:
    """Ghi 1 artifact mẫu vào slot bằng đúng đường store.write (ACL + schema) rồi trả facts."""

    uses_llm = True

    def __init__(
        self, stage: Stage, slot: str, role: str, build: Builder, overrides: Mapping[str, Any]
    ) -> None:
        self.stage = stage
        self.slot = slot
        self.role = role
        self._build = build
        self._o = dict(overrides)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        ctx.cancel.raise_if_cancelled()
        asked = self._maybe_escalate(state, ctx)
        if asked is not None:
            return asked
        data = self._build(state, ctx, {k: v for k, v in self._o.items() if k != "escalate"})
        ref = ctx.svc.store.write(state.issue_id, self.slot, data, actor=Actor(role=self.role))
        key = self.slot.rstrip("/").split(".")[0]
        summary = {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))}
        facts: dict[str, Any] = {key: summary, "outcome": {"kind": "artifact", "uri": ref.uri}}
        if self.slot == "decision.json":  # harness (risk.confidence B11) — stub lấy từ decision
            facts["confidence"] = data["confidence"]
        return StageResult(stage=self.stage, facts=facts, usage=_STUB_USAGE)

    def _maybe_escalate(self, state: IssueState, ctx: StageContext) -> StageResult | None:
        """overrides[slot]["escalate"] = {question, options, recommendation, type?, gate?, times?}: nộp escalation
        thay cho artifact cho tới khi người đã trả lời `times` lần ở stage này. Cùng đường với AgentStage."""
        cfg = self._o.get("escalate")
        if not isinstance(cfg, Mapping):
            return None
        answered = [
            a
            for a in state.facts.get("human_answers") or []
            if isinstance(a, Mapping) and a.get("stage") == self.stage.value
        ]
        if len(answered) >= int(cfg.get("times", 1)):
            return None
        now = ctx.clock.now()
        form: dict[str, Any] = {
            "issue_id": state.issue_id,
            "raised_by": self.role,
            "stage": self.stage.value,
            "type": cfg.get("type", "ambiguity"),
            "question": cfg.get(
                "question", "Spec có hai cách hiểu, cần người chọn một hướng trước khi làm tiếp?"
            ),
            "options": cfg.get(
                "options",
                [
                    {"id": "A", "action": "Làm theo cách hiểu thứ nhất", "cost": "~5 phút", "risk": "low"},
                    {"id": "B", "action": "Làm theo cách hiểu thứ hai", "cost": "~15 phút", "risk": "medium"},
                ],
            ),
            "recommendation": cfg.get("recommendation", "A"),
            "confidence": cfg.get("confidence", 0.6),
            "blocked_since": now.isoformat(),
        }
        for key in ("gate", "default_on_timeout", "recommendation_reason"):
            if cfg.get(key):
                form[key] = cfg[key]
        ref = ctx.svc.store.write(state.issue_id, "escalation.yaml", form, actor=Actor(role=self.role))
        raised = on_escalation(state, ctx.svc.store, now=now, stage=self.stage, role=self.role)
        if raised is None:  # quá giới hạn, harness đã tự chọn → làm artifact như thường
            return None
        facts = {"outcome": {"kind": "escalation", "uri": ref.uri, "reason": None}, **raised}
        return StageResult(stage=self.stage, facts=facts, usage=_STUB_USAGE)


class StubMachineStage:
    """Stage 0 LLM: MACHINE_GATE · MERGE — chỉ trả facts."""

    uses_llm = False

    def __init__(
        self,
        stage: Stage,
        facts_fn: Callable[[IssueState, dict[str, Any]], dict[str, Any]],
        overrides: Mapping[str, Any],
    ) -> None:
        self.stage = stage
        self._fn = facts_fn
        self._o = dict(overrides)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        return result(self.stage, **self._fn(state, self._o))


def _next_version(ctx: StageContext, issue: str, slot: str) -> int:
    ref = ctx.svc.store.latest(issue, slot)
    return ref.version + 1 if ref else 1


def _mg(state: IssueState, o: dict[str, Any]) -> dict[str, Any]:
    fail = state.mg_retries < int(o.get("fail_times", 0))
    failures = (
        [
            {
                "gate": "unit_test",
                "summary": "1 failed, 4 passed in 0.12s",
                "cmd": "pytest -q",
                "failed_tests": [
                    {"name": "tests/test_calc.py::test_mean_empty", "message": "ZeroDivisionError"}
                ],
            }
        ]
        if fail
        else []
    )
    return {
        "machine_gate": {
            "any_fail": fail,
            "build": "pass",
            "lint": "pass",
            "unit_test": "fail" if fail else "pass",
        },
        "mg_failures": failures,
    }


def _merge(state: IssueState, o: dict[str, Any]) -> dict[str, Any]:
    conflict = bool(o.get("conflict", False)) and state.rework < 1
    return {"merge": {"conflict": conflict, "commit": None if conflict else f"stub{state.attempt:03d}"}}


def stub_handlers(overrides: Mapping[str, Mapping[str, Any]] | None = None) -> dict[Stage, StageHandler]:
    ov: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (overrides or {}).items()}

    def o(k: str) -> dict[str, Any]:
        return ov.get(k, {})

    return {
        Stage.TRIAGE: StubAgentStage(
            Stage.TRIAGE, "intake.yaml", "intake", lambda s, c, x: sd.stub_intake(s.issue_id, x), o("intake")
        ),
        Stage.LOCALIZE: StubAgentStage(
            Stage.LOCALIZE, "plan.yaml", "planner", lambda s, c, x: sd.stub_plan(s.issue_id, x), o("plan")
        ),
        Stage.SPEC: StubAgentStage(
            Stage.SPEC,
            "spec.yaml",
            "analyst",
            lambda s, c, x: sd.stub_spec(s.issue_id, x, tier=s.tier),
            o("spec"),
        ),
        Stage.IMPLEMENT: StubAgentStage(
            Stage.IMPLEMENT,
            "patch/",
            "dev",
            lambda s, c, x: sd.stub_patch(s.issue_id, x, attempt=s.attempt),
            o("patch"),
        ),
        Stage.MACHINE_GATE: StubMachineStage(Stage.MACHINE_GATE, _mg, o("machine_gate")),
        Stage.VERIFY: StubAgentStage(
            Stage.VERIFY,
            "evidence.json",
            "verifier",
            lambda s, c, x: sd.stub_evidence(
                s.issue_id, x, version=_next_version(c, s.issue_id, "evidence.json")
            ),
            o("evidence"),
        ),
        Stage.REVIEW: StubAgentStage(
            Stage.REVIEW,
            "sec_report.json",
            "security",
            lambda s, c, x: sd.stub_sec_report(s.issue_id, x),
            o("sec_report"),
        ),
        Stage.GATE: StubAgentStage(
            Stage.GATE,
            "decision.json",
            "leader",
            lambda s, c, x: sd.stub_decision(s.issue_id, x, tier=s.tier, rework=s.rework),
            o("decision"),
        ),
        Stage.MERGE: StubMachineStage(Stage.MERGE, _merge, o("merge")),
        Stage.ANSWER: AnswerStage(
            StubAgentStage(
                Stage.ANSWER, "answer.yaml", "dev", lambda s, c, x: sd.stub_answer(s.issue_id, x), o("answer")
            )
        ),
    }

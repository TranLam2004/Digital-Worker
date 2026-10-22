"""GATE — harness tính confidence (0 LLM) → tier trong policy.gate_rule_tiers (mặc định T0) sạch: ghi decision.json
theo rule (Leader không được gọi) · còn lại: Leader (AgentStage) đọc confidence_breakdown trong brief và quyết."""

from __future__ import annotations

from typing import Any

from digital_development_team.core.context import Actor
from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import StoreError
from digital_development_team.core.models import StageResult
from digital_development_team.memory.outcomes import familiarity
from digital_development_team.risk.confidence import compute_confidence
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.base import StageHandler, inner_model_role
from digital_development_team.runner.state import IssueState

RUNNER = Actor(role="runner")


class GateStage:
    stage = Stage.GATE
    uses_llm = True
    role = "leader"

    def __init__(self, leader: StageHandler) -> None:
        self._leader = leader

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        return inner_model_role(self._leader, state, cfg, self.role)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        issue = state.issue_id
        store = ctx.svc.store
        evidence = self._slot(ctx, issue, "evidence.json")
        spec = self._slot(ctx, issue, "spec.yaml")
        sec = self._slot(ctx, issue, "sec_report.json") or None
        fam = self._familiarity(ctx, issue)
        conf = compute_confidence(
            ctx.config.policy.confidence_weights,
            evidence=evidence,
            spec=spec,
            sec_report=sec,
            rework=state.rework,
            reloc=state.reloc,
            **({"familiarity": fam} if fam is not None else {}),
        )
        state.merge_facts(conf.to_facts())
        chk_raw = state.facts.get("checklist")
        chk: dict[str, Any] = dict(chk_raw) if isinstance(chk_raw, dict) else {}
        policy = ctx.config.policy
        rule_only = (
            state.tier.value in policy.gate_rule_tiers
            and evidence.get("verdict") == "pass"
            and bool(chk.get("clean", True))
            and (sec is None or sec.get("verdict") == "pass")
            and conf.value >= policy.effective_conf_proceed()
            and not evidence.get("scope_violation")
        )
        if rule_only:
            decision = self._template_decision(issue, state, conf.value, conf.breakdown, evidence, sec)
            ref = store.write(issue, "decision.json", decision, actor=RUNNER)
            facts: dict[str, Any] = {
                **conf.to_facts(),
                "decision": {k: v for k, v in decision.items() if isinstance(v, (str, int, float, bool))},
                "outcome": {"kind": "artifact", "uri": ref.uri, "rule_only": True},
                "gate_rule_only": True,
            }
            return StageResult(stage=self.stage, facts=facts)
        result = self._leader.run(state, ctx)
        merged = {**result.facts, **conf.to_facts()}  # harness tính confidence — Leader chỉ đọc
        return StageResult(stage=self.stage, facts=merged, usage=result.usage, outcome=result.outcome)

    def _familiarity(self, ctx: StageContext, issue: str) -> float | None:
        """Tỷ lệ pass thật của Dev trên module (role_outcomes). Chưa có → familiarity_unknown của preset autonomy
        (thi đấu: repo mới toanh, 0.4 kéo T0 hoàn hảo xuống dưới ngưỡng) → None = 0.4. --no-learn tắt lịch sử."""
        policy = ctx.config.policy
        known: float | None = None
        if not policy.no_learn:
            module = str(self._slot(ctx, issue, "intake.yaml").get("affected_area") or "")
            known = familiarity(ctx.svc.db, module=module) if module else None
        if known is not None:
            return known
        preset = policy.effective_autonomy()
        return preset.familiarity_unknown if preset is not None else None

    @staticmethod
    def _slot(ctx: StageContext, issue: str, slot: str) -> dict[str, Any]:
        try:
            return dict(ctx.svc.store.read(issue, slot).content)
        except StoreError:
            return {}

    @staticmethod
    def _template_decision(
        issue: str,
        state: IssueState,
        confidence: float,
        breakdown: dict[str, float],
        evidence: dict[str, Any],
        sec: dict[str, Any] | None,
    ) -> dict[str, Any]:
        criteria = evidence.get("criteria") or []
        passed = sum(1 for c in criteria if isinstance(c, dict) and c.get("result") == "pass")
        files = (state.facts.get("machine_gate") or {}).get("files_touched") or []
        return {
            "issue_id": issue,
            "action": "merge",
            "final_tier": state.tier.value,
            "tier_bumped_because": list(state.facts.get("tier_bumped_because") or []),
            "confidence": confidence,
            "confidence_breakdown": breakdown,
            "requires_human": False,
            "rationale": (
                f"{state.tier.value} sạch: Verifier pass {passed}/{len(criteria)} AC, regression {(evidence.get('regression') or {}).get('result', '?')}, "
                f"checklist regex 0 blocking, security {'pass' if sec else 'skipped (regex sạch)'}. Harness quyết theo rule, Leader không gọi."
            ),
            "summary_for_human": {
                "ac_pass_count": f"{passed}/{len(criteria)}",
                "files_changed": len(files),
                "regression": str((evidence.get("regression") or {}).get("result") or "skipped"),
                "security": "pass" if sec else "n/a",
                "rework_count": state.rework,
                "tokens_used": state.usage.tokens,
                "top_risk": "T0 — rủi ro thấp",
            },
            "standup_line": f"{issue} — merged T0 auto (rule), {state.rework} rework, {state.usage.tokens} tok",
        }

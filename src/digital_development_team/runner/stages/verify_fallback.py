"""VERIFY khi Verifier không làm được: lần đầu blocked → transitions cho chạy lại một lần (bump verify_retries);
lần hai vẫn blocked → harness ghi evidence.json `inconclusive` (0 LLM) thay vì để issue BLOCKED hoặc đọc nhầm
evidence cũ của vòng trước. Inconclusive → GATE → G1 bắt buộc người (gates.g1.requires_human_when).
"""

from __future__ import annotations

from typing import Any

from digital_development_team.core.context import Actor
from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import DDTError, StoreError
from digital_development_team.core.models import AgentOutcome, StageResult
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.base import StageHandler, inner_model_role
from digital_development_team.runner.state import IssueState

SLOT = "evidence.json"


class VerifyFallback:
    stage = Stage.VERIFY
    uses_llm = True

    def __init__(self, inner: StageHandler) -> None:
        self._inner = inner
        self.role = getattr(inner, "role", None)

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        return inner_model_role(self._inner, state, cfg, self.role)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        res = self._inner.run(state, ctx)
        out = res.facts.get("outcome")
        retries = int((state.facts.get("counters") or {}).get("verify_retries", 0))
        if not isinstance(out, dict) or out.get("kind") != "blocked" or retries < 1:
            return res
        reason = str(out.get("reason") or "Verifier blocked")
        uri = _write_inconclusive(state, ctx, reason)
        if uri is None:
            return res
        note = f"harness: Verifier blocked twice ({reason[:160]}) → evidence inconclusive"
        facts = {
            **res.facts,
            "outcome": {"kind": "artifact", "uri": uri, "reason": note},
            "evidence": {"verdict": "inconclusive"},
        }
        return res.model_copy(update={"facts": facts, "outcome": AgentOutcome.artifact(uri, res.usage)})


def _write_inconclusive(state: IssueState, ctx: StageContext, reason: str) -> str | None:
    svc = ctx.svc
    issue = state.issue_id
    try:
        spec = svc.store.read(issue, "spec.yaml").content
    except StoreError:
        return None
    ids = [str(ac.get("id")) for ac in spec.get("acceptance_criteria", []) if isinstance(ac, dict)]
    if not ids:
        return None
    harness = svc.harness_fields.get(issue, SLOT) if svc.harness_fields is not None else {}
    latest = svc.store.latest(issue, SLOT)
    mg = harness.get("machine_gate") or _machine_gate_from_facts(state.facts.get("machine_gate"))
    evidence: dict[str, Any] = {
        "issue_id": issue,
        "version": harness.get("version") or ((latest.version + 1) if latest else 1),
        "verdict": "inconclusive",
        "criteria": [
            {"id": i, "result": "unclear", "evidence": f"Verifier could not run: {reason}"[:300]} for i in ids
        ],
        "regression": {"ran": False, "result": "skipped", "skip_reason": "Verifier blocked twice"},
        "machine_gate": mg,
        "unverifiable": ids,
    }
    for k in ("checklist_results", "scope_violation"):
        if k in harness:
            evidence[k] = harness[k]
    try:
        return svc.store.write(issue, SLOT, evidence, actor=Actor(role="runner")).uri
    except DDTError:
        return None


def _machine_gate_from_facts(mg: Any) -> dict[str, Any]:
    src = mg if isinstance(mg, dict) else {}
    out: dict[str, Any] = {}
    for k in ("build", "lint", "typecheck", "unit_test"):
        status = src.get(k)
        out[k] = {"status": status if status in ("pass", "fail", "skipped") else "skipped"}
    return out

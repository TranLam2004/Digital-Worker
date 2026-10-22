"""Pydantic View — chỉ ĐỌC artifact đã validate bằng JSON Schema. extra='ignore', frozen.

KHÔNG dùng View để ghi. Contract test kiểm View ⊆ schema (tên field, required, enum).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from digital_development_team.core.enums import Complexity, IssueType, Route, Severity, Tier


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


# ───── intake.yaml ─────
class IntakeView(_View):
    issue_id: str
    type: IssueType
    severity: Severity
    complexity: Complexity
    risk_tier: Tier
    route: Route
    confidence: float
    affected_area: str
    entry_hints: list[str] = Field(default_factory=list)
    test_command_guess: str | None = None
    requires_human_before_code: bool = False
    duplicate_of: str | None = None
    rationale: str
    question: str | None = None


# ───── plan.yaml ─────
class PlanFileRef(_View):
    path: str
    symbols: list[str] = Field(default_factory=list)
    lines: str | None = None


class PlanCandidate(_View):
    rank: int
    files: list[PlanFileRef]
    confidence: float
    reason: str
    callers_affected: list[str] = Field(default_factory=list)


class PlanStep(_View):
    order: int
    action: Literal["write_failing_test", "modify", "add", "refactor", "run_test", "verify_regression"]
    target: str
    note: str | None = None


class PlanView(_View):
    issue_id: str
    localize_method: list[str] = Field(default_factory=list)
    candidates: list[PlanCandidate]
    steps: list[PlanStep]
    hot_files_touched: list[str] = Field(default_factory=list)
    rollback: str
    estimated_complexity: Complexity | None = None
    needs_containment: bool = False
    containment_proposal: str | None = None

    @property
    def primary_files(self) -> list[str]:
        return [f.path for f in self.candidates[0].files] if self.candidates else []


# ───── spec.yaml ─────
class AcceptanceCriterion(_View):
    id: str
    text: str
    verify_by: Literal["test", "lint", "typecheck", "diff_inspection", "manual"]
    test_ref: str | None = None
    mvs_ids: list[str] = Field(default_factory=list)
    test_command: str | None = None


class Ambiguity(_View):
    question: str
    assumed: str


class SpecView(_View):
    issue_id: str
    goal: str
    acceptance_criteria: list[AcceptanceCriterion]
    allowed_files: list[str]
    forbidden_files: list[str] = Field(default_factory=list)
    test_command: str | None = None  # bắt buộc theo task profile (preflight), không phải schema
    regression_command: str | None = None
    rollback_plan: str
    out_of_scope: list[str] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    estimated_tier: Tier | None = None


# ───── evidence.json ─────
class GateResult(_View):
    status: Literal["pass", "fail", "skipped"]
    output_ref: str | None = None
    summary: str | None = None


class MachineGate(_View):
    build: GateResult
    lint: GateResult
    typecheck: GateResult | None = None
    unit_test: GateResult
    sast: GateResult | None = None
    coverage: GateResult | None = None
    coverage_delta: float | None = None
    coverage_total: float | None = None
    uncovered_new_lines: list[dict[str, Any]] | None = None
    blast_radius: dict[str, Any] | None = None


class CriterionResult(_View):
    id: str
    result: Literal["pass", "fail", "unclear"]
    evidence: str
    evidence_type: Literal["test_output", "file_line", "lint_output", "diff_inspection"] | None = None


class Regression(_View):
    ran: bool
    result: Literal["pass", "fail", "skipped"]
    evidence: str | None = None
    failed_tests: list[str] = Field(default_factory=list)
    skip_reason: str | None = None


class AdversarialFinding(_View):
    severity: Literal["blocking", "major", "minor", "nit"]
    location: str
    description: str
    suggested_fix: str | None = None


class ChecklistResult(_View):
    id: str
    result: Literal["pass", "fail", "n/a"]
    evidence: str | None = None
    by: Literal["harness", "verifier"] | None = None
    severity: Literal["blocking", "major", "minor", "nit"] | None = None


class EvidenceView(_View):
    issue_id: str
    version: int
    verdict: Literal["pass", "fail", "inconclusive"]
    machine_gate: MachineGate
    criteria: list[CriterionResult]
    regression: Regression
    adversarial_findings: list[AdversarialFinding] = Field(default_factory=list)
    adversarial_attempted: list[str] = Field(default_factory=list)
    checklist_results: list[ChecklistResult] = Field(default_factory=list)
    unverifiable: list[str] = Field(default_factory=list)
    failure_feedback_for_dev: str | None = None
    scope_violation: list[str] = Field(default_factory=list)
    handoff: str | None = None

    @property
    def blocking_findings(self) -> list[AdversarialFinding]:
        return [f for f in self.adversarial_findings if f.severity == "blocking"]


# ───── sec_report.json ─────
class SecCheck(_View):
    result: Literal["pass", "fail", "n/a"]
    evidence: str | None = None


class SecFinding(_View):
    severity: Literal["critical", "high", "medium", "low", "info"]
    cwe_or_category: str
    location: str
    description: str
    remediation: str | None = None


class SecReportView(_View):
    issue_id: str
    mode: Literal["lite", "full", "incident_5c"]
    verdict: Literal["pass", "fail", "needs_human"]
    checklist: dict[str, SecCheck] = Field(default_factory=dict)
    findings: list[SecFinding] = Field(default_factory=list)
    redaction_applied: bool | None = None
    escalate_reason: str | None = None


# ───── decision.json ─────
class ConfidenceBreakdown(_View):
    test_signal: float
    verifier_agreement: float
    spec_clarity: float
    familiarity: float
    rework_penalty: float


class DecisionView(_View):
    issue_id: str
    action: Literal["merge", "rework", "re_localize", "escalate", "block", "supersede"]
    final_tier: Tier
    tier_bumped_because: list[str] = Field(default_factory=list)
    confidence: float
    confidence_breakdown: ConfidenceBreakdown
    requires_human: bool
    human_timeout_sec: int | None = None
    rationale: str
    summary_for_human: dict[str, Any] = Field(default_factory=dict)
    rework_instructions: str | None = None
    escalation_ref: str | None = None
    superseded_by: str | None = None
    standup_line: str | None = None


# ───── escalation.yaml ─────
class EscalationOption(_View):
    id: str
    action: str
    cost: str
    risk: Literal["low", "medium", "high"]
    consequence: str | None = None
    route: Literal["continue", "rework", "re_localize", "merge", "abort"] | None = None


class EscalationResolved(_View):
    by: str
    at: str
    choice: str
    note: str | None = None
    chosen_action: str | None = None
    route: Literal["continue", "rework", "re_localize", "merge", "abort"] | None = None


class EscalationView(_View):
    issue_id: str
    raised_by: str
    stage: str
    gate: str | None = None
    type: Literal["ambiguity", "risk", "capability", "conflict", "destructive_request", "budget"]
    question: str
    options: list[EscalationOption]
    recommendation: str
    recommendation_reason: str | None = None
    confidence: float
    blocked_since: str
    timeout_sec: int | None = None
    default_on_timeout: str | None = None
    resolved: EscalationResolved | None = None


# ───── clarify.yaml ─────
class ClarifyOption(_View):
    id: str
    interpretation: str


class ClarifyAnswer(_View):
    by: str
    choice: str
    note: str | None = None
    spec_patch: Any = None
    answered_at: str


class ClarifyView(_View):
    issue_id: str
    from_: str = Field(alias="from")
    to: str
    about: str
    question: str
    options: list[ClarifyOption]
    leaning: str | None = None
    asked_at: str
    answer: ClarifyAnswer | None = None
    timeout_sec: int = 60

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


# ───── answer.yaml ─────
class AnswerFollowUp(_View):
    title: str
    type: Literal["bug", "change_request"]
    reason: str | None = None


class AnswerView(_View):
    issue_id: str
    answer: str
    references: list[str]
    confidence: float
    follow_up: list[AnswerFollowUp] = Field(default_factory=list)


VIEWS: dict[str, type[_View]] = {
    "intake": IntakeView,
    "plan": PlanView,
    "spec": SpecView,
    "evidence": EvidenceView,
    "sec_report": SecReportView,
    "decision": DecisionView,
    "escalation": EscalationView,
    "clarify": ClarifyView,
    "answer": AnswerView,
}

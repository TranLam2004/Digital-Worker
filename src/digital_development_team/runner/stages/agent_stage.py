"""AgentStage — stage gọi một agent: worktree (nếu cần) → brief → AgentRuntime.run → facts.

Dùng cho TRIAGE · LOCALIZE · SPEC · IMPLEMENT · VERIFY · REVIEW · GATE. Stage KHÔNG tự chuyển; trả outcome.kind
để transitions quyết (vd IMPLEMENT: outcome.kind == 'blocked' → BLOCKED).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from digital_development_team.agents.brief import BriefInputs
from digital_development_team.core.context import Actor, RunContext
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import ConfigError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import AgentOutcome, BudgetUsage, StageResult
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.spec_amend import auto_amend_from_escalation
from digital_development_team.runner.stages.escalation import MAX_ASKS, on_escalation
from digital_development_team.runner.state import IssueState

NEEDS_WORKTREE = {Stage.IMPLEMENT, Stage.VERIFY, Stage.REVIEW}


class AgentStage:
    uses_llm = True

    def __init__(self, stage: Stage, role: str) -> None:
        self.stage = stage
        self.role = role

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        """Role trong models.yaml dùng cho lần chạy này: `<role>_rework` khi rework đã tới ngưỡng và role đó có
        cấu hình; không thì role của agent. (Thang phục hồi sẽ đặt model mạnh hơn qua facts.recovery.)"""
        rec = state.facts.get("recovery")
        if isinstance(rec, dict) and rec.get("model_role") and rec.get("model_stage") == self.stage.value:
            return str(rec["model_role"])
        rework_role = f"{self.role}_rework"
        if (
            self.stage is Stage.IMPLEMENT
            and state.rework >= cfg.policy.rework_model_escalation_at
            and rework_role in cfg.models.roles
        ):
            return rework_role
        return self.role

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        svc = ctx.svc
        if svc.agents is None or svc.brief_builder is None or svc.agent_runtime is None:
            raise ConfigError(
                f"stage {self.stage.value} cần agent runtime",
                hint="chạy với --repo (repo đích) hoặc --stub-stages",
            )
        spec = svc.agents.get(self.role).for_stage(self.stage)  # vd Dev ở ANSWER: hồ sơ chỉ đọc
        issue = state.issue_id

        runs_code = self.stage is Stage.ANSWER and any(
            t.startswith(("exec.", "test.")) for t in spec.tool_names
        )
        if self.stage in NEEDS_WORKTREE or runs_code:  # spike ở ANSWER chạy được lệnh chỉ đọc
            ensure_worktree(state, ctx, self.stage)

        fs_root: str | None = state.worktree if (self.stage in NEEDS_WORKTREE or runs_code) else None
        if fs_root is None and svc.worktrees is not None:
            fs_root = str(
                svc.worktrees.git.root
            )  # stage chỉ đọc (LOCALIZE/SPEC): đọc thẳng repo, không cần worktree
        allowed = self._allowed_files(ctx, issue)
        budget_ref = ctx.budget.ref if ctx.budget is not None else None
        if budget_ref is None:
            raise ConfigError(f"stage {self.stage.value} không có budget trong budgets.yaml")
        model_role = self.model_role_for(state, ctx.config)
        if model_role.endswith("_rework") and not spec.rework_model_escalation:
            model_role = self.role
        rctx = RunContext(
            issue_id=issue,
            role=spec.name,
            stage=self.stage,
            tier=state.tier,
            budget=budget_ref,
            cancel=ctx.cancel,
            clock=ctx.clock,
            actor=Actor(role=spec.name),
            attempt=state.attempt,
            rework=state.rework,
            worktree=fs_root,
            allowed_files=allowed,
            permission_mode=spec.permission_mode,
            model_role=None if model_role == self.role else model_role,
        )
        usage = BudgetUsage()
        # lần sau chỉ khi: harness tự duyệt spec_patch của agent, hoặc agent hỏi quá MAX_ASKS và harness tự chọn khuyến nghị
        for _ in range(3):
            brief = svc.brief_builder.build(
                spec,
                BriefInputs(
                    issue_id=issue,
                    stage=self.stage,
                    tier=state.tier,
                    budget=budget_ref,
                    attempt=state.attempt,
                    rework=state.rework,
                    facts=state.facts,
                ),
            )
            self._note_brief_usage(state, svc, brief)
            outcome = svc.agent_runtime.run(spec, brief, rctx)
            usage = usage + outcome.usage
            if outcome.kind != "escalation":
                break
            if auto_amend_from_escalation(state, ctx, self.stage):
                rctx = dataclasses.replace(rctx, allowed_files=self._allowed_files(ctx, issue))
                continue
            raised = on_escalation(
                state, ctx.svc.store, now=ctx.clock.now(), stage=self.stage, role=self.role
            )
            if raised is not None:
                return StageResult(
                    stage=self.stage,
                    facts={**_outcome_facts(outcome), **raised},
                    usage=usage,
                    outcome=outcome,
                )
        else:  # vẫn hỏi sau khi đã có câu trả lời tự động
            outcome = AgentOutcome.blocked(f"TOO_MANY_ASKS: {self.role} hỏi quá {MAX_ASKS} lần", usage)
        facts = _outcome_facts(outcome)
        if outcome.kind == "artifact":
            facts.update(self._artifact_summary(ctx, issue, spec.output_slot))
        return StageResult(stage=self.stage, facts=facts, usage=usage, outcome=outcome)

    def _note_brief_usage(self, state: IssueState, svc: Any, brief: Any) -> None:
        """Ghi role này dùng bao nhiêu skill học được và fact memory (đo skill_lift, memory_lift ở màn Đánh giá)."""
        trace = getattr(svc.brief_builder, "last_trace", None)
        rels = list(getattr(trace, "skill_rels", []) or [])
        usage = dict(state.facts.get("brief_usage") or {})
        refs = [str(r) for r in (getattr(brief, "memory_refs", ()) or ())]
        usage[self.role] = {
            "learned_skills": sum(1 for r in rels if "/learned/" in f"/{r}"),
            "memory_refs": len(refs),
            "refs": refs[:20],  # harness chấm fact/skill này khi issue xong (runner/learn.py apply_usage)
        }
        state.facts["brief_usage"] = usage

    @staticmethod
    def _allowed_files(ctx: StageContext, issue: str) -> tuple[str, ...]:
        try:
            sp = ctx.svc.store.read(issue, "spec.yaml").content
        except StoreError:
            return ()
        return tuple(str(x) for x in sp.get("allowed_files", []))

    @staticmethod
    def _artifact_summary(ctx: StageContext, issue: str, slot: str) -> dict[str, Any]:
        try:
            content = ctx.svc.store.read(issue, slot).content
        except StoreError:
            return {}
        key = slot.rstrip("/").split(".")[0]
        return {key: {k: v for k, v in content.items() if isinstance(v, (str, int, float, bool))}}


def ensure_worktree(state: IssueState, ctx: StageContext, stage: Stage) -> None:
    """Worktree của issue (tạo nếu chưa có, dùng lại nếu có) → state.worktree/branch/base_commit."""
    svc = ctx.svc
    if svc.worktrees is None:
        raise ConfigError("stage này cần worktree nhưng không có repo git", hint="truyền --repo")
    issue = state.issue_id
    info = svc.worktrees.ensure(issue, base_commit=state.base_commit)
    state.worktree, state.branch, state.base_commit = str(info.path), info.branch, info.base_commit
    if info.created:
        svc.sink.emit(
            EventDraft(
                issue_id=issue,
                actor="runner",
                event=EventType.WORKTREE_CREATED,
                stage=stage,
                data={
                    "path": svc.layout.rel(info.path),
                    "branch": info.branch,
                    "base": info.base_commit[:12],
                },
            )
        )


def _outcome_facts(outcome: AgentOutcome) -> dict[str, Any]:
    uri = outcome.artifact_uri or outcome.escalation_uri
    out: dict[str, Any] = {"outcome": {"kind": outcome.kind, "uri": uri, "reason": outcome.reason}}
    if outcome.kind == "artifact" and uri:
        out["outcome"]["slot"] = uri.split("/", 3)[-1].rsplit("@", 1)[0]  # blackboard://ISSUE/<slot>@v
    return out

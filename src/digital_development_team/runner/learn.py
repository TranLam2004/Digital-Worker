"""Tự học sau mỗi issue (50-knowledge), không nằm trên đường chính.

1. apply_usage   — 0 LLM: fact/skill học được đã vào brief của issue vừa xong → chấm điểm theo kết quả role đã dùng nó
                   (pass · soft_fail khi phải rework · fail khi BLOCKED/người bác). Tự nâng candidate → active, tự gỡ.
2. learn_reasons — 0 LLM: issue có đáng học không. Có lý do → một dòng learn_jobs.
3. LearnRunner   — Learner (LLM) xử lý job khi worker rảnh, budget LEARN, chỉ blackboard.read + memory.*; ghi
                   learn_report.json. Lỗi chỉ ghi log + đánh dấu job, không bao giờ chặn pipeline.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from digital_development_team.config.models import MemoryCfg
from digital_development_team.core.context import Actor, BudgetRef, CancelToken, RunContext
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import ConfigError, DDTError
from digital_development_team.core.events import Event, EventDraft
from digital_development_team.core.models import Checkpoint
from digital_development_team.memory.facts import FactStore
from digital_development_team.memory.learned import LearnedSkills, Verdict
from digital_development_team.memory.outcomes import RecordResult
from digital_development_team.runner.context import Services

log = logging.getLogger(__name__)

LEARNER = "learner"
LEARN_BUDGET = "LEARN"
EXTERNAL_TOOLS = ("web.", "docs.", "pkg.", "mcp.", "skill.load", "llm.ask")


def _human_fix(events: Sequence[Event]) -> bool:
    return any(
        (e.event is EventType.GATE_RESOLVED and e.human and str(e.human.choice).startswith("reject"))
        or e.event is EventType.HUMAN_OVERRIDE
        for e in events
    )


def verdict_for(passed: bool | None, *, outcome: str, human_fix: bool) -> Verdict | None:
    if passed is None:
        return None
    if passed:
        return "pass"
    return "fail" if outcome == "blocked" or human_fix else "soft_fail"


def _usage(cp: Checkpoint) -> dict[str, Any]:
    u = cp.facts.get("brief_usage")
    return dict(u) if isinstance(u, dict) else {}


def apply_usage(
    *,
    skills: LearnedSkills,
    facts: FactStore,
    cfg: MemoryCfg,
    checkpoint: Checkpoint,
    result: RecordResult,
    events: Sequence[Event],
) -> list[str]:
    """Chấm fact/skill học được theo role đã nhận chúng trong brief. Trả mô tả đổi trạng thái (log + event)."""
    human = _human_fix(events)
    changes: list[str] = []
    for role, u in _usage(checkpoint).items():
        refs = [str(r) for r in (u.get("refs") or []) if isinstance(r, str)] if isinstance(u, dict) else []
        ro = result.roles.get(role)
        if not refs or ro is None:
            continue
        v = verdict_for(ro.passed, outcome=result.outcome, human_fix=human)
        if v is None:
            continue
        changes += skills.record_use(refs, issue=checkpoint.issue_id, verdict=v)
        changes += facts.record_use(refs, issue=checkpoint.issue_id, verdict=v, cfg=cfg)
    return changes


def learn_reasons(
    *,
    checkpoint: Checkpoint,
    events: Sequence[Event],
    result: RecordResult,
    cfg: MemoryCfg,
    same_passes: int,
) -> list[str]:
    """Vì sao issue này đáng để Learner xem. Rỗng → bỏ qua (issue trơn tru, không có gì mới)."""
    cp = checkpoint
    if not result.roles:  # chưa agent nào chạy (vd bị chặn lúc INGEST)
        return []
    reasons: list[str] = []
    done = result.outcome == "done"
    tools = [e.tool.name for e in events if e.event is EventType.TOOL_CALL and e.tool is not None]
    if not done:
        reasons.append("blocked")
    if done and cp.rework > 0:
        reasons.append("rework_then_pass")
    if _human_fix(events):
        reasons.append("human_fix")
    if done and len(tools) >= cfg.learn_min_tool_calls:
        reasons.append("long_run")
    if any(t.startswith(EXTERNAL_TOOLS) for t in tools):
        reasons.append("external_lookup")
    if done and same_passes >= 2:
        reasons.append("repeat_pattern")
    learned_used = any(
        any(str(r).startswith("skill:learned-") for r in (u.get("refs") or []))
        for u in _usage(cp).values()
        if isinstance(u, dict)
    )
    if learned_used and (not done or cp.rework > 0):
        reasons.append("learned_skill_struggled")
    return reasons


def enqueue(svc: Services, issue: str, reasons: Sequence[str]) -> bool:
    if not reasons:
        return False
    now = svc.clock.now().isoformat()
    with svc.db.transaction() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO learn_jobs(issue_id, reasons, status, created_at, updated_at)"
            " VALUES(?,?,'pending',?,?)",
            (issue, json.dumps(list(reasons)), now, now),
        )
        return bool(cur.rowcount)


def same_type_passes(svc: Services, module: str, issue_type: str) -> int:
    return int(
        svc.db.scalar(
            "SELECT COUNT(*) FROM episodes WHERE module=? AND issue_type=? AND outcome='pass'",
            (module, issue_type),
        )
        or 0
    )


def jobs(svc: Services, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    rows = svc.db.all(
        "SELECT issue_id, reasons, status, attempts, note, created_at, updated_at FROM learn_jobs"
        " WHERE (? IS NULL OR status=?) ORDER BY created_at LIMIT ?",
        (status, status, limit),
    )
    return [{**dict(r), "reasons": json.loads(r["reasons"] or "[]")} for r in rows]


class LearnRunner:
    """Chạy Learner cho một job. Worker gọi khi rảnh; `ddt learn run` gọi tay."""

    def __init__(self, svc: Services) -> None:
        self._svc = svc

    def next_job(self) -> str | None:
        r = self._svc.db.one(
            "SELECT issue_id FROM learn_jobs WHERE status='pending' AND attempts < ? ORDER BY created_at LIMIT 1",
            (self._svc.config.policy.memory.learn_max_attempts,),
        )
        return str(r["issue_id"]) if r is not None else None

    def run_one(self, cancel: CancelToken, *, issue: str | None = None) -> tuple[str, str] | None:
        """(issue, status: done · failed · pending · skipped) hoặc None khi không có job."""
        svc = self._svc
        issue = issue or self.next_job()
        if issue is None:
            return None
        job = svc.db.one("SELECT reasons, attempts FROM learn_jobs WHERE issue_id=?", (issue,))
        if job is None:
            return None
        if svc.config.policy.no_learn:
            self._mark(issue, "skipped", "--no-learn")
            return issue, "skipped"
        reasons = json.loads(job["reasons"] or "[]")
        attempts = int(job["attempts"]) + 1
        self._mark(issue, "running", None, attempts=attempts)
        try:
            outcome = self._run(issue, reasons, cancel)
        except Exception as e:  # Learner hỏng không được ảnh hưởng việc khác
            status = "pending" if attempts < svc.config.policy.memory.learn_max_attempts else "failed"
            self._mark(issue, status, f"{type(e).__name__}: {e}"[:300])
            self._emit(issue, {"status": status, "error": str(e)[:300], "reasons": reasons})
            log.warning("%s: Learner lỗi (%s): %s", issue, status, e)
            return issue, status
        status = "done" if outcome.kind == "artifact" else "failed"
        if status == "failed" and attempts < svc.config.policy.memory.learn_max_attempts:
            status = "pending"
        self._mark(issue, status, outcome.reason)
        self._emit(
            issue,
            {"status": status, "kind": outcome.kind, "reasons": reasons, "tokens": outcome.usage.tokens},
        )
        return issue, status

    def _run(self, issue: str, reasons: list[str], cancel: CancelToken) -> Any:
        svc = self._svc
        if svc.agents is None or svc.brief_builder is None or svc.agent_runtime is None:
            raise ConfigError("Learner cần agent runtime", hint="chạy worker với --repo")
        cp = svc.checkpoints.load(issue)
        if cp is None:
            raise ConfigError(f"không có checkpoint {issue}")
        spec = svc.agents.get(LEARNER)
        stage = Stage(cp.stage.value) if cp.stage.terminal else Stage.DONE
        sb = svc.config.budgets.stages.get(LEARN_BUDGET)
        if sb is None:
            raise ConfigError("budgets.yaml thiếu stage LEARN")
        b = sb.for_tier(cp.tier)
        ref = BudgetRef(turns=max(b.turn, 1), wall_s=b.wall * 60.0, tokens=max(b.tokens, 1))
        if svc.models is not None and svc.models.overhead_key_for(LEARNER) != "api":
            from digital_development_team.budget.resolve import apply_transport_overhead

            ref = apply_transport_overhead(ref, svc.config.budgets, svc.models.overhead_key_for(LEARNER))
        facts = {**cp.facts, "learn": self._context(cp, reasons)}
        from digital_development_team.agents.brief import BriefInputs

        brief = svc.brief_builder.build(
            spec,
            BriefInputs(
                issue_id=issue, stage=stage, tier=cp.tier, budget=ref, attempt=1, rework=0, facts=facts
            ),
        )
        rctx = RunContext(
            issue_id=issue,
            role=LEARNER,
            stage=stage,
            tier=cp.tier,
            budget=ref,
            cancel=cancel,
            clock=svc.clock,
            actor=Actor(role=LEARNER),
            attempt=1,
            rework=0,
            worktree=None,
            allowed_files=(),
            permission_mode=spec.permission_mode,
        )
        return svc.agent_runtime.run(spec, brief, rctx)

    def _context(self, cp: Checkpoint, reasons: list[str]) -> dict[str, Any]:
        """Tóm tắt do harness dựng cho Learner: kết quả, vì sao đáng học, đã dùng gì, lý do rework/chặn."""
        from digital_development_team.eventlog.reader import read_events

        events = read_events(self._svc.layout, cp.issue_id)
        tools: dict[str, int] = {}
        for e in events:
            if e.event is EventType.TOOL_CALL and e.tool is not None:
                tools[e.tool.name] = tools.get(e.tool.name, 0) + 1
        reworks = [str(e.data.get("reason"))[:300] for e in events if e.event is EventType.REWORK][-3:]
        notes = [
            f"{e.human.gate}: {e.human.choice} — {str(e.human.note)[:300]}"
            for e in events
            if e.event is EventType.GATE_RESOLVED and e.human and e.human.note
        ][-3:]
        intake = cp.facts.get("intake") if isinstance(cp.facts.get("intake"), Mapping) else {}
        usage = {
            role: [r for r in (u.get("refs") or []) if isinstance(r, str)]
            for role, u in _usage(cp).items()
            if isinstance(u, dict) and u.get("refs")
        }
        out: dict[str, Any] = {
            "outcome": cp.status.value,
            "why_learn": reasons,
            "type": (intake or {}).get("type"),
            "module": (intake or {}).get("affected_area"),
            "rework": cp.rework,
            "tools_used": dict(sorted(tools.items(), key=lambda kv: -kv[1])[:15]),
        }
        if reworks:
            out["rework_reasons"] = reworks
        if notes:
            out["human_notes"] = notes
        if usage:
            out["memory_used"] = usage
        return out

    def _mark(self, issue: str, status: str, note: str | None, *, attempts: int | None = None) -> None:
        now = self._svc.clock.now().isoformat()
        with self._svc.db.transaction() as c:
            if attempts is None:
                c.execute(
                    "UPDATE learn_jobs SET status=?, note=?, updated_at=? WHERE issue_id=?",
                    (status, note, now, issue),
                )
            else:
                c.execute(
                    "UPDATE learn_jobs SET status=?, note=?, attempts=?, updated_at=? WHERE issue_id=?",
                    (status, note, attempts, now, issue),
                )

    def _emit(self, issue: str, data: dict[str, Any]) -> None:
        try:
            self._svc.sink.emit(
                EventDraft(issue_id=issue, actor=LEARNER, event=EventType.LEARN_RUN, data=data)
            )
        except DDTError as e:  # pragma: no cover
            log.warning("%s: ghi event learn_run lỗi: %s", issue, e)


__all__ = [
    "LearnRunner",
    "apply_usage",
    "enqueue",
    "jobs",
    "learn_reasons",
    "same_type_passes",
    "verdict_for",
]

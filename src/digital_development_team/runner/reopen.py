"""Mở lại issue BLOCKED — trước đây BLOCKED là ngõ cụt (retry chỉ cho issue tạm dừng, ingest lại bị dedupe).

Người (CLI `ddt reopen`, web, lệnh worker `reopen`) chọn một option của blocked_report.json hoặc tự chỉ định:
  stage đích · ghi chú cho agent · file thêm vào allowlist (qua runner/spec_amend.py) · % budget cấp thêm.
Harness: kiểm còn lượt (recovery.max_reopens) → áp grant → câu trả lời của người vào human_answers + retry_feedback
cho stage đích → reset bộ đếm rework/re-localize/thang phục hồi (lịch sử giữ lại) → status PAUSED ở stage đích.
`ddt run` hoặc worker chạy tiếp từ worktree cũ (worktree còn nguyên sau BLOCKED).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from digital_development_team.config.models import PolicyConfig
from digital_development_team.core.enums import EventType, IssueStatus, Stage
from digital_development_team.core.errors import DDTError
from digital_development_team.core.events import EventDraft
from digital_development_team.ingest.queue import Queue
from digital_development_team.memory.outcomes import forget_outcome
from digital_development_team.runner.context import Services
from digital_development_team.runner.feedback import FACT as RETRY_FACT
from digital_development_team.runner.spec_amend import amend_spec
from digital_development_team.runner.state import IssueState

REOPEN_STAGES = frozenset(
    {Stage.TRIAGE, Stage.LOCALIZE, Stage.SPEC, Stage.IMPLEMENT, Stage.ANSWER, Stage.GATE}
)


def reopen_issue(
    svc: Services,
    policy: PolicyConfig,
    issue: str,
    *,
    by: str,
    stage: str | None = None,
    option: str | None = None,
    note: str | None = None,
    allow: Sequence[str] = (),
    budget_pct: int = 0,
) -> tuple[bool, dict[str, Any]]:
    cp = svc.checkpoints.load(issue)
    if cp is None or cp.status is not IssueStatus.BLOCKED:
        status = cp.status.value if cp else "chưa chạy"
        return False, {"error": f"{issue} đang {status}; chỉ mở lại được issue BLOCKED"}
    state = IssueState.from_checkpoint(cp)
    reopens = int(state.facts.get("reopens", 0) or 0)
    if reopens >= policy.recovery.max_reopens:
        return False, {"error": f"{issue} đã mở lại {reopens} lần (tối đa {policy.recovery.max_reopens})"}
    report = _report(svc, issue)
    opt = _option(report, option)
    if option and opt is None:
        return False, {"error": f"blocked_report không có option {option}"}
    grant: Mapping[str, Any] = (opt.get("grant") or {}) if opt else {}
    target_raw = stage or (opt.get("reopen_at") if opt else None) or _default_stage(report)
    try:
        target = Stage(str(target_raw).upper())
    except ValueError:
        return False, {"error": f"stage lạ {target_raw!r}"}
    if target not in REOPEN_STAGES:
        return False, {
            "error": f"không mở lại ở {target.value}; chọn một trong {sorted(s.value for s in REOPEN_STAGES)}"
        }

    # spec: thêm file / đổi lệnh do người cấp
    files = [*map(str, grant.get("allowlist_add") or []), *allow]
    patch: dict[str, Any] = {}
    if files:
        patch["allowed_files_add"] = files
    if grant.get("test_command"):
        patch["test_command"] = str(grant["test_command"])
    amended: list[str] = []
    if patch:
        root = Path(state.worktree) if state.worktree else (svc.worktrees.git.root if svc.worktrees else None)
        if root is None:
            return False, {"error": "không có repo để kiểm đường dẫn"}
        res = amend_spec(
            svc.store,
            svc.sink,
            issue,
            patch,
            by=by,
            reason=f"reopen: {note or ''}"[:300],
            root=Path(root),
            guard=policy.command_guard,
            max_files=policy.spec_amend.max_allowed_files,
            at=svc.clock.now().isoformat(),
            stage=target,
        )
        if not res.ok:
            return False, {"error": "spec không sửa được: " + "; ".join(res.problems)}
        amended = res.added
    pct = int(grant.get("budget_pct") or 0) + max(0, int(budget_pct))
    if pct:
        state.facts["budget_grant_pct"] = int(state.facts.get("budget_grant_pct", 0) or 0) + pct

    action = (opt.get("action") if opt else None) or f"reopen at {target.value}"
    summary = str(report.get("summary") or "") if report else ""
    answers = list(state.facts.get("human_answers") or [])
    answers.append(
        {
            "stage": target.value,
            "raised_by": "runner",
            "question": f"Issue was BLOCKED ({report.get('category') if report else '?'}): {summary[:200]}",
            "choice": option or "reopen",
            "chosen_action": action,
            "route": "continue",
            "by": by,
            "note": note,
            "wait_sec": None,
            "override": False,
        }
    )
    state.facts["human_answers"] = answers
    lines = [f"- the previous run ended BLOCKED at {report.get('stage') if report else '?'}: {summary[:400]}"]
    if note:
        lines.append(f"- {by} reopened it at {target.value}: {note}")
    if amended:
        lines.append("- now allowed: " + ", ".join(amended))
    state.facts[RETRY_FACT] = {"from": "BLOCKED", "to": target.value, "text": "\n".join(lines), "refs": []}

    rec = dict(state.facts.get("recovery") or {})
    rec["counts"] = {}  # bậc phục hồi dùng lại được sau khi người gỡ
    rec.pop("model_role", None)
    rec.pop("model_stage", None)
    state.facts["recovery"] = rec
    state.facts["counters"] = {}
    for k in ("outcome", "outcomes", "_stage_entered", "failure_class", "cancelled"):
        state.facts.pop(k, None)
    state.facts["reopens"] = reopens + 1
    state.rework = state.reloc = state.mg_retries = 0
    state.stage, state.phase, state.status = target, "ready", IssueStatus.PAUSED
    state.pending_gate = None
    state.set_parallel(())
    svc.checkpoints.save(state.to_checkpoint(saved_at=svc.clock.now()))
    forget_outcome(svc.db, issue)  # kết quả BLOCKED cũ không còn là kết quả cuối → lần kết thúc sau ghi lại
    ev = svc.sink.emit(
        EventDraft(
            issue_id=issue,
            actor="runner",
            event=EventType.REOPENED,
            stage=target,
            data={
                "by": by,
                "to": target.value,
                "option": option,
                "note": (note or "")[:300],
                "allowed_files_add": amended,
                "budget_grant_pct": pct,
                "reopens": reopens + 1,
            },
        )
    )
    try:
        cp2 = svc.checkpoints.load(issue)
        if cp2 is not None:
            svc.checkpoints.save(cp2.model_copy(update={"last_seq": ev.seq}))
    except DDTError:
        pass
    queue = Queue(svc.db, svc.clock)
    if queue.get(issue) is not None:
        queue.set_status(issue, "queued")
    return True, {
        "stage": target.value,
        "reopens": reopens + 1,
        "allowed_files_add": amended,
        "budget_pct": pct,
    }


def _report(svc: Services, issue: str) -> dict[str, Any]:
    try:
        return dict(svc.store.read(issue, "blocked_report.json").content)
    except DDTError:
        return {}


def _option(report: Mapping[str, Any], option: str | None) -> Mapping[str, Any] | None:
    opts = [o for o in report.get("unblock_options") or [] if isinstance(o, Mapping)]
    want = option or (str(report.get("recommendation")) if report.get("recommendation") else None)
    return next((o for o in opts if str(o.get("id")) == want), None) if want else None


def _default_stage(report: Mapping[str, Any]) -> str:
    st = str(report.get("stage") or "")
    return st if st in {s.value for s in REOPEN_STAGES} else "IMPLEMENT"

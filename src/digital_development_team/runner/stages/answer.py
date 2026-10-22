"""ANSWER — fast path cho type=question (10-harness/01-runner): TRIAGE → ANSWER → DONE, bỏ LOCALIZE…VERIFY.

Dev chạy ở hồ sơ `stage_modes.ANSWER` (chỉ đọc, ghi answer.yaml), đọc câu hỏi Intake đã viết lại và REPO.md.
Bước "Security kiểm" là quét tất định 0 LLM: câu trả lời có chuỗi giống secret → BLOCKED, người xem trước khi gửi.
"""

from __future__ import annotations

from typing import Any

from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import DDTError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import AgentOutcome, StageResult
from digital_development_team.policy.hooks import redact_secrets
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.base import StageHandler, inner_model_role
from digital_development_team.runner.state import IssueState

SLOT = "answer.yaml"


class AnswerStage:
    stage = Stage.ANSWER
    uses_llm = True

    def __init__(self, writer: StageHandler) -> None:
        self._writer = writer  # AgentStage(ANSWER, "dev") hoặc stub
        self.role = getattr(
            writer, "role", None
        )  # runner cộng transport_overhead theo role (CLI chậm hơn API)

    def model_role_for(self, state: IssueState, cfg: Any) -> str:
        return inner_model_role(self._writer, state, cfg, self.role)

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        had_worktree = bool(state.worktree)
        try:
            result = self._writer.run(state, ctx)
        finally:
            if not had_worktree:
                _drop_worktree(state, ctx)  # worktree chỉ để chạy lệnh chỉ đọc khi điều tra — không giữ lại
        if (result.facts.get("outcome") or {}).get("kind") != "artifact":
            return result  # escalation · blocked: transitions xử lý
        try:
            answer = dict(ctx.svc.store.read(state.issue_id, SLOT).content)
        except StoreError:
            return _blocked(result, "answer.yaml không đọc được sau khi ghi")
        text = "\n".join([str(answer.get("answer", "")), *map(str, answer.get("references", []))])
        _, leaks = redact_secrets(text)
        summary: dict[str, Any] = {
            "confidence": answer.get("confidence"),
            "references": len(answer.get("references", [])),
            "follow_up": len(answer.get("follow_up", [])),
            "chars": len(str(answer.get("answer", ""))),
            "secret_hits": leaks,
        }
        facts = {k: v for k, v in result.facts.items() if k != "answer"}  # bỏ bản tóm tắt chứa cả đoạn văn
        facts["answer"] = summary
        if leaks:
            return _blocked(
                result.model_copy(update={"facts": facts}),
                f"câu trả lời có {leaks} chuỗi giống secret, cần người xem trước khi gửi",
            )
        return result.model_copy(update={"facts": facts})


def _blocked(result: StageResult, reason: str) -> StageResult:
    facts = dict(result.facts)
    facts["outcome"] = {"kind": "blocked", "uri": None, "reason": reason}
    return result.model_copy(update={"facts": facts, "outcome": AgentOutcome.blocked(reason, result.usage)})


def _drop_worktree(state: IssueState, ctx: StageContext) -> None:
    svc = ctx.svc
    if not state.worktree or svc.worktrees is None:
        return
    try:
        svc.worktrees.remove(state.issue_id, force=True)
    except DDTError:
        return
    svc.sink.emit(
        EventDraft(
            issue_id=state.issue_id,
            actor="runner",
            event=EventType.WORKTREE_REMOVED,
            stage=Stage.ANSWER,
            data={"path": state.worktree},
        )
    )
    state.worktree = state.branch = state.base_commit = None

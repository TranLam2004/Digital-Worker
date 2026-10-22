"""Core models. Frozen, extra=forbid. Không I/O.

Artifact nội dung là dict đã validate JSON Schema (nguồn sự thật). Pydantic ở đây là kiểu nội bộ của harness.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from digital_development_team.core.context import Actor, BudgetRef
from digital_development_team.core.enums import GateId, IssueStatus, OutputMode, Stage, Tier
from digital_development_team.core.ids import IssueId, RepoPath, Role, SlotName


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ───── Issue ─────
class Issue(_Frozen):
    """= item.json. Harness sở hữu. body luôn bọc <untrusted_input> khi vào brief."""

    id: IssueId
    source: str
    source_id: str
    title: str
    body: str
    labels: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    priority_hint: str | None = None
    blocked_by: tuple[IssueId, ...] = ()
    received_at: AwareDatetime
    raw: dict[str, Any] = Field(default_factory=dict)
    deadline_at: AwareDatetime | None = None  # hạn nộp theo đồng hồ server (Game Master); None = không hạn


# ───── Artifact ─────
class ArtifactRef(_Frozen):
    issue_id: IssueId
    slot: SlotName
    version: int = Field(ge=1)
    path: str  # POSIX, tương đối home: work/DDT-0001/spec.v2.yaml
    sha256: str
    written_by: str
    written_at: AwareDatetime

    @property
    def uri(self) -> str:
        return f"blackboard://{self.issue_id}/{self.slot}@{self.version}"


class Artifact(_Frozen):
    ref: ArtifactRef
    content: dict[str, Any]

    def view[V: BaseModel](self, cls: type[V]) -> V:
        return cls.model_validate(self.content)


class PatchBundle(_Frozen):
    """Slot thư mục patch/. Không có JSON Schema → Pydantic + diff_sanity hook."""

    diff: str
    files_touched: tuple[RepoPath, ...]
    impl_log: str = Field(max_length=2000)
    base_commit: str
    head_commit: str
    handoff: str | None = Field(default=None, max_length=300)

    @property
    def is_empty(self) -> bool:
        return not self.diff.strip() or not self.files_touched


# ───── Budget ─────
CACHED_TOKEN_WEIGHT = 0.1
"""Token cache-read tính vào trục `tokens` của budget theo trọng số này (giá cache-read ≈ 10% input). tokens_in vẫn
lưu nguyên để báo cáo; chỉ trục budget dùng giá trị quy đổi. Lý do: transport CLI gửi lại ~15-20k token system prompt
mỗi lượt dưới dạng cache-read — tính 1:1 làm Dev chạm trần sau 6 lượt dù chi phí thật rất nhỏ."""


class BudgetUsage(_Frozen):
    turns: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    wall_s: float = 0.0
    cost_usd: float = 0.0

    @property
    def tokens(self) -> int:
        """Token tính budget: uncached input + output + cache-read × CACHED_TOKEN_WEIGHT."""
        cached = min(self.tokens_cached, self.tokens_in)
        return self.tokens_in - cached + round(cached * CACHED_TOKEN_WEIGHT) + self.tokens_out

    def __add__(self, o: BudgetUsage) -> BudgetUsage:
        return BudgetUsage(
            turns=self.turns + o.turns,
            llm_calls=self.llm_calls + o.llm_calls,
            tokens_in=self.tokens_in + o.tokens_in,
            tokens_out=self.tokens_out + o.tokens_out,
            tokens_cached=self.tokens_cached + o.tokens_cached,
            wall_s=self.wall_s + o.wall_s,
            cost_usd=self.cost_usd + o.cost_usd,
        )


# ───── Brief ─────
class Constraints(_Frozen):
    allowed_files: tuple[RepoPath, ...] = ()
    budget: BudgetRef
    forbidden: tuple[str, ...] = ("push", "xóa file", "sửa CI config", "đọc .env", "network ra ngoài")
    extra: tuple[str, ...] = ()


class OutputContract(_Frozen):
    slot: SlotName
    schema_id: str | None  # None cho patch/
    mode: OutputMode
    summary: str  # schema rút gọn: required + enum + description 1 dòng


class Brief(_Frozen):
    """Gói 4 khối gửi agent. render() dùng template _assets/prompts/brief.md."""

    issue_id: IssueId
    role: Role
    stage: Stage
    attempt: int = Field(ge=1)
    context: str
    task: str
    constraints: Constraints
    output: OutputContract
    sources: tuple[str, ...] = ()  # ArtifactRef.uri đã đọc — provenance
    memory_refs: tuple[str, ...] = ()  # fact/skill id đã nạp — để Learner demote


# ───── Tool call / result ─────
class ToolCall(_Frozen):
    id: str
    name: str  # canonical fs.edit
    args: dict[str, Any]
    actor: Actor
    issue_id: IssueId
    stage: Stage

    def args_hash(self) -> str:
        return sha256_text(canonical_json(self.args))


class ToolError(_Frozen):
    code: str
    message: str
    hint: str | None = None
    details: list[dict[str, str]] | None = None


class ToolResult(_Frozen):
    status: Literal["ok", "error", "denied"]
    summary: str = Field(max_length=300)
    structured: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    preview: tuple[str, ...] = Field(default=(), max_length=20)
    error: ToolError | None = None

    @model_validator(mode="after")
    def _error_iff_not_ok(self) -> Self:
        if (self.status == "ok") == (self.error is not None):
            raise ValueError("error bắt buộc khi status != ok và cấm khi ok")
        return self

    @staticmethod
    def ok(summary: str, **structured: Any) -> ToolResult:
        return ToolResult(status="ok", summary=summary, structured=structured)

    @staticmethod
    def fail(
        code: str, message: str, *, hint: str | None = None, summary: str | None = None, **structured: Any
    ) -> ToolResult:
        return ToolResult(
            status="error",
            summary=summary or f"{code}: {message}"[:300],
            structured=structured,
            error=ToolError(code=code, message=message, hint=hint),
        )

    @staticmethod
    def denied(rule_id: str, message: str, *, hint: str | None = None, **structured: Any) -> ToolResult:
        return ToolResult(
            status="denied",
            summary=f"denied: {message}"[:300],
            structured={"rule": rule_id, **structured},
            error=ToolError(code="POLICY_DENIED", message=message, hint=hint),
        )

    def to_model_text(self) -> str:
        """Nội dung đưa vào tool_result block cho model."""
        payload: dict[str, Any] = {"status": self.status, "summary": self.summary}
        if self.structured:
            payload["structured"] = self.structured
        if self.artifact_refs:
            payload["artifact_refs"] = list(self.artifact_refs)
        if self.preview:
            payload["preview"] = list(self.preview)
        if self.error:
            payload["error"] = self.error.model_dump(exclude_none=True)
        return json.dumps(payload, ensure_ascii=False, indent=None)


# ───── Sandbox ─────
class SandboxProfile(_Frozen):
    name: Literal["dev", "verifier", "scan"]
    writable: tuple[str, ...]
    cmd_allowlist: tuple[str, ...] | None = None
    network: bool = False


class ExecResult(_Frozen):
    exit_code: int
    stdout_path: str
    stderr_path: str
    stdout_tail: tuple[str, ...]
    stderr_tail: tuple[str, ...]
    stdout_lines: int
    stderr_lines: int
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False


# ───── Agent outcome / Stage result ─────
class AgentOutcome(_Frozen):
    kind: Literal["artifact", "escalation", "blocked"]
    usage: BudgetUsage
    artifact_uri: str | None = None
    escalation_uri: str | None = None
    reason: str | None = None

    @staticmethod
    def artifact(uri: str, usage: BudgetUsage) -> AgentOutcome:
        return AgentOutcome(kind="artifact", usage=usage, artifact_uri=uri)

    @staticmethod
    def blocked(reason: str, usage: BudgetUsage) -> AgentOutcome:
        return AgentOutcome(kind="blocked", usage=usage, reason=reason)

    @staticmethod
    def escalation(uri: str, usage: BudgetUsage) -> AgentOutcome:
        return AgentOutcome(kind="escalation", usage=usage, escalation_uri=uri)


class StageResult(_Frozen):
    """Stage trả facts cho TransitionTable. KHÔNG tự chuyển stage."""

    stage: Stage
    facts: dict[str, Any] = Field(default_factory=dict)
    usage: BudgetUsage = BudgetUsage()
    outcome: AgentOutcome | None = None


# ───── Checkpoint ─────
class Checkpoint(_Frozen):
    schema_version: Literal[1] = 1
    issue_id: IssueId
    stage: Stage
    phase: Literal["ready", "stage_done"]
    status: IssueStatus
    tier: Tier = Tier.T1
    rework: int = 0
    reloc: int = 0
    rejections: int = 0
    mg_retries: int = 0
    attempt: int = 1
    facts: dict[str, Any] = Field(default_factory=dict)
    worktree: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    locks: tuple[str, ...] = ()
    pending_gate: str | None = None
    session_approvals: tuple[str, ...] = ()
    usage: BudgetUsage = BudgetUsage()
    leader_ctx_ref: str | None = None
    last_seq: int = -1
    saved_at: AwareDatetime

    @property
    def terminal(self) -> bool:
        return self.status.terminal


# ───── Gate ─────
class GateRequest(_Frozen):
    issue_id: IssueId
    gate: GateId
    attempt: int
    tier: Tier
    summary: dict[str, Any]
    escalation_uri: str | None = None
    timeout_s: float | None  # None = không timeout


class GateTicket(_Frozen):
    id: str
    issue_id: IssueId
    gate: GateId
    opened_at: AwareDatetime
    timeout_s: float | None


class GateDecision(_Frozen):
    choice: str  # approve | reject | reject_final | A | B | other | timeout
    by: str
    at: AwareDatetime
    note: str | None = None
    is_timeout: bool = False


# ───── Subagent ─────
class SubagentRequest(_Frozen):
    task: str
    files: tuple[RepoPath, ...]
    context: str | None = None
    tools: tuple[str, ...] | None = None
    budget_pct: float | None = None
    must_test: str | None = None


class SubagentResult(_Frozen):
    sub_id: str
    status: Literal["done", "blocked", "timeout", "stopped"]
    files_changed: tuple[str, ...] = ()
    test_output: str | None = None
    summary: str | None = None
    reason: str | None = None
    usage: BudgetUsage = BudgetUsage()

"""Enum dùng chung. StrEnum để serialize thẳng ra JSON/YAML và so sánh với chuỗi trong config.

Contract test `tests/contract/test_event_schema_sync.py` giữ Stage và EventType đồng bộ với
`_assets/schemas/event.schema.json`.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class Stage(StrEnum):
    """Mọi trạng thái của state machine, kể cả trạng thái chờ và kết thúc."""

    INGEST = "INGEST"
    TRIAGE = "TRIAGE"
    G0_WAIT = "G0_WAIT"
    LOCALIZE = "LOCALIZE"
    SPLIT = "SPLIT"
    SPEC = "SPEC"
    IMPLEMENT = "IMPLEMENT"
    MACHINE_GATE = "MACHINE_GATE"
    VERIFY = "VERIFY"
    REVIEW = "REVIEW"
    GATE = "GATE"
    G1_WAIT = "G1_WAIT"
    MERGE = "MERGE"
    ANSWER = "ANSWER"
    DONE = "DONE"
    BLOCKED = "BLOCKED"

    @property
    def terminal(self) -> bool:
        return self in (Stage.DONE, Stage.BLOCKED)

    @property
    def is_wait(self) -> bool:
        return self in (Stage.G0_WAIT, Stage.G1_WAIT)


@unique
class Tier(StrEnum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"

    @property
    def rank(self) -> int:
        return int(self.value[1])

    def bump(self, n: int = 1) -> Tier:
        return Tier(f"T{min(3, self.rank + n)}")

    @staticmethod
    def max_of(*tiers: Tier | None) -> Tier:
        """MAX các tier, bỏ qua None. Rỗng → T1 (mặc định)."""
        present = [t for t in tiers if t is not None]
        return max(present, key=lambda t: t.rank) if present else Tier.T1


@unique
class PermissionMode(StrEnum):
    READONLY = "readonly"
    ACCEPT_EDITS = "accept_edits"
    ASK_ALL = "ask_all"
    MEMORY_ONLY = "memory_only"


@unique
class RiskTier(StrEnum):
    """Mức rủi ro của một tool op."""

    READ = "read"
    SOFT_WRITE = "soft_write"
    HARD_WRITE = "hard_write"
    DESTRUCTIVE = "destructive"
    PAUSE = "pause"


@unique
class OutputMode(StrEnum):
    """Agent trả artifact bằng cách nào.

    tool_write: gọi blackboard.write — Dev, Planner, Verifier.
    final_message: harness tách JSON từ câu trả lời cuối rồi tự write — Intake, Analyst, Leader (1 lượt).
    """

    TOOL_WRITE = "tool_write"
    FINAL_MESSAGE = "final_message"


@unique
class GateId(StrEnum):
    G0 = "G0"
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"


@unique
class IssueStatus(StrEnum):
    RUNNING = "running"
    WAITING_GATE = "waiting_gate"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DONE = "done"

    @property
    def terminal(self) -> bool:
        return self in (IssueStatus.BLOCKED, IssueStatus.DONE)


@unique
class IssueType(StrEnum):
    BUG = "bug"
    CHANGE_REQUEST = "change_request"
    SECURITY_INCIDENT = "security_incident"
    PRODUCTION_INCIDENT = "production_incident"
    ISSUE = "issue"
    QUESTION = "question"
    REFACTOR = "refactor"
    DOCS = "docs"
    TEST = "test"
    CONFIG = "config"
    DEPENDENCY = "dependency"
    PERF = "perf"
    CHORE = "chore"
    SPIKE = "spike"


@unique
class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


@unique
class Complexity(StrEnum):
    TRIVIAL = "trivial"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    UNKNOWN = "unknown"


@unique
class Route(StrEnum):
    FAST = "fast"
    STANDARD = "standard"
    SECURITY = "security"
    HUMAN_FIRST = "human_first"


@unique
class EventType(StrEnum):
    INGESTED = "ingested"
    STAGE_ENTER = "stage_enter"
    STAGE_EXIT = "stage_exit"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_DENIED = "tool_denied"
    SLOT_WRITTEN = "slot_written"
    SLOT_REJECTED = "slot_rejected"
    SLOT_VERSIONED = "slot_versioned"
    SPEC_AMENDED = "spec_amended"
    RECOVERY = "recovery"
    REOPENED = "reopened"
    GATE_WAIT = "gate_wait"
    GATE_RESOLVED = "gate_resolved"
    GATE_TIMEOUT = "gate_timeout"
    ESCALATION_RAISED = "escalation_raised"
    REWORK = "rework"
    RE_LOCALIZE = "re_localize"
    BLOCKED = "blocked"
    BUDGET_WARNING = "budget_warning"
    CIRCUIT_BREAKER = "circuit_breaker"
    LOCK_ACQUIRED = "lock_acquired"
    LOCK_BLOCKED = "lock_blocked"
    LOCK_RELEASED = "lock_released"
    LOCK_EXPIRED = "lock_expired"
    WORKTREE_CREATED = "worktree_created"
    WORKTREE_REMOVED = "worktree_removed"
    MERGED = "merged"
    MERGE_CONFLICT = "merge_conflict"
    CANARY_INJECTED = "canary_injected"
    CANARY_RESULT = "canary_result"
    SUBAGENT_SPAWNED = "subagent_spawned"
    SUBAGENT_DONE = "subagent_done"
    SUBAGENT_REVIEWED = "subagent_reviewed"
    SUBAGENT_STOPPED = "subagent_stopped"
    CLARIFY_ASKED = "clarify_asked"
    CLARIFY_ANSWERED = "clarify_answered"
    CHECKLIST_RUN = "checklist_run"
    CHECKLIST_LEARNED = "checklist_learned"
    CHECKLIST_DEMOTED = "checklist_demoted"
    RETRO_GENERATED = "retro_generated"
    PEER_REVIEW_DISAGREE = "peer_review_disagree"
    MEMORY_WRITE = "memory_write"
    MEMORY_PROMOTE = "memory_promote"
    MEMORY_DEMOTE = "memory_demote"
    LEARN_RUN = "learn_run"
    STUCK_DETECTED = "stuck_detected"
    CONTEXT_CONDENSED = "context_condensed"
    ISSUE_SPLIT = "issue_split"
    HUMAN_OVERRIDE = "human_override"
    STOP_REQUESTED = "stop_requested"
    RESUMED = "resumed"
    ISSUE_DONE = "issue_done"
    ISSUE_SUPERSEDED = "issue_superseded"
    CONFIG_RELOADED = "config_reloaded"
    CONFIG_ERROR = "config_error"
    # thi đấu (Game Master)
    MATCH_ITEM = "match_item"  # item mới · đề đổi · CLOSED · bỏ vì không kịp
    MATCH_STARTED = "match_started"  # start working: nhận template (+ MVS)
    MATCH_SUBMITTED = "match_submitted"
    MATCH_GRADED = "match_graded"

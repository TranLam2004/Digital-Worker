"""Game Master wire models: accept known field aliases, never invent operational state.

Spec API chính thức chưa có (Workshop 2) — mọi chỗ đoán tên field tập trung ở `_pick` của file này.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from digital_development_team.core.errors import DDTError


class GameMasterProtocolError(DDTError):
    code = "GAMEMASTER_PROTOCOL"


class MatchStatus(StrEnum):
    READY = "READY"
    PREPARING = "PREPARING"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"

    @property
    def over(self) -> bool:
        return self in (MatchStatus.FINISHED, MatchStatus.CANCELLED)


class ItemType(StrEnum):
    TASK = "TASK"
    CR = "CR"
    BUG = "BUG"
    PI = "PI"

    @property
    def intake_type(self) -> str:
        """Loại issue trong DDT (intake.schema) — TRIAGE không phải đoán."""
        return {"TASK": "issue", "CR": "change_request", "BUG": "bug", "PI": "production_incident"}[
            self.value
        ]

    @property
    def has_mvs(self) -> bool:
        return self in (ItemType.CR, ItemType.BUG)


class ItemStatus(StrEnum):
    VISIBLE = "VISIBLE"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class GradeStatus(StrEnum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BacklogItem(_M):
    id: str  # {TYPE}-{số}
    type: ItemType
    title: str = ""
    description: str = ""
    status: ItemStatus = ItemStatus.VISIBLE
    points: float | None = None
    revealed_at: datetime | None = None  # giờ server
    deadline_at: datetime | None = None  # giờ server; None = không hạn
    raw: dict[str, Any] = Field(default_factory=dict)

    def fingerprint(self) -> tuple[str, str, float | None]:
        """Phần đề mà đổi thì phải lập lại kế hoạch (title · description · điểm). Hạn KHÔNG tính: trận PAUSED làm
        hạn lùi lại — chỉ cập nhật match.json, không làm lại từ đầu."""
        return (self.title, self.description, self.points)


class MatchSnapshot(_M):
    match_id: str
    status: MatchStatus
    server_time: datetime
    ends_at: datetime | None = None
    brief: dict[str, Any] = Field(default_factory=dict)  # applicationBrief: name · summary · mainFeatures
    repository: dict[str, Any] = Field(default_factory=dict)
    items: tuple[BacklogItem, ...] = ()
    exam_info: dict[str, Any] = Field(default_factory=dict)

    def item(self, item_id: str) -> BacklogItem | None:
        return next((i for i in self.items if i.id == item_id), None)


class StartResult(_M):
    item_id: str
    submit_template: dict[str, Any] = Field(default_factory=dict)
    mvs: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = Field(default_factory=dict)


class SubmitResult(_M):
    item_id: str
    submission_id: str
    status: str  # ACCEPTED | REJECTED
    provisional_score: float | None = None
    message: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status.upper() == "ACCEPTED" and bool(self.submission_id.strip())


class GradeResult(_M):
    item_id: str
    submission_id: str
    status: GradeStatus
    score: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# ───── parse khoan dung ─────
def _pick(d: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def parse_time(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):  # epoch giây hoặc mili-giây
        try:
            return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def parse_item(d: Mapping[str, Any]) -> BacklogItem:
    item_id = str(_pick(d, "id", "itemId", "backlogId", "code", default=""))
    raw_type = str(_pick(d, "type", "itemType", default=item_id.split("-", 1)[0])).upper()
    status = str(_pick(d, "status", default="VISIBLE")).upper()
    if not item_id.strip() or raw_type not in ItemType.__members__ or status not in ItemStatus.__members__:
        raise GameMasterProtocolError("Backlog item requires a valid id, type and status")
    revealed = parse_time(_pick(d, "revealedAt", "revealed_at", "visibleAt", "openedAt"))
    deadline = parse_time(_pick(d, "deadlineAt", "deadline_at", "closesAt", "deadline"))
    minutes = _pick(d, "deadlineMinutes", "deadline_minutes")
    if deadline is None and revealed is not None and isinstance(minutes, int | float):
        deadline = revealed + timedelta(minutes=float(minutes))
    points = _pick(d, "points", "maxPoints", "max_points", "score")
    return BacklogItem(
        id=item_id,
        type=ItemType(raw_type),
        title=str(_pick(d, "title", default="") or ""),
        description=str(_pick(d, "description", "descriptionMarkdown", "body", default="") or ""),
        status=ItemStatus(status),
        points=float(points) if isinstance(points, int | float) else None,
        revealed_at=revealed,
        deadline_at=deadline,
        raw=dict(d),
    )


def parse_snapshot(
    d: Mapping[str, Any], *, match_id: str, local_now: datetime | None = None
) -> MatchSnapshot:
    # local_now is retained for adapter compatibility; it is never an authoritative clock.
    items_raw: Sequence[Any] = _pick(d, "backlog", "items", "backlogItems", default=[])
    status = str(_pick(d, "status", "state", default="")).upper()
    server_time = parse_time(_pick(d, "serverTime", "server_time", "now"))
    returned_id = str(_pick(d, "id", "matchId", default=match_id))
    if status not in MatchStatus.__members__ or server_time is None or returned_id != match_id:
        raise GameMasterProtocolError(
            "Match response requires a known status, server time and matching match id"
        )
    if not isinstance(items_raw, list | tuple) or any(not isinstance(x, Mapping) for x in items_raw):
        raise GameMasterProtocolError("Match backlog must be an array of objects")
    end_raw = _pick(d, "endsAt", "ends_at", "matchDeadlineAt")
    ends_at = parse_time(end_raw)
    if end_raw is not None and ends_at is None:
        raise GameMasterProtocolError("Invalid match end time")
    return MatchSnapshot(
        match_id=returned_id,
        status=MatchStatus(status),
        server_time=server_time,
        ends_at=ends_at,
        brief=dict(_pick(d, "applicationBrief", "application_brief", "brief", default={}) or {}),
        repository=dict(_pick(d, "repository", "repo", "gitRepo", default={}) or {}),
        items=tuple(parse_item(x) for x in items_raw if isinstance(x, Mapping)),
        exam_info=dict(_pick(d, "examInfo", "exam_info", default={}) or {}),
    )


def parse_start(d: Mapping[str, Any], item_id: str) -> StartResult:
    mvs = _pick(d, "mvs", "minimumVerificationScenarios", "scenarios", default=[])
    template = _pick(d, "submitTemplate", "submit_template", "template")
    if not isinstance(template, Mapping) or not isinstance(mvs, list | tuple):
        raise GameMasterProtocolError("Start Working requires a template object and a scenarios array")
    return StartResult(
        item_id=item_id,
        submit_template=dict(template),
        mvs=tuple(x if isinstance(x, dict) else {"scenario": x} for x in mvs),
        raw=dict(d),
    )


def parse_submit(d: Mapping[str, Any], item_id: str) -> SubmitResult:
    status = str(_pick(d, "status", "result", default="")).upper()
    sid = str(_pick(d, "submissionId", "submission_id", "id", default="")).strip()
    if status not in ("ACCEPTED", "REJECTED") or (status == "ACCEPTED" and not sid):
        raise GameMasterProtocolError(
            "Submission response requires an explicit status and acceptance receipt"
        )
    return SubmitResult(
        item_id=item_id,
        submission_id=sid,
        status=status,
        provisional_score=_pick(d, "provisionalScore", "provisional_score", "score"),
        message=str(_pick(d, "message", "error", default="") or ""),
        raw=dict(d),
    )


def parse_grade(d: Mapping[str, Any], item_id: str, submission_id: str) -> GradeResult:
    raw = str(_pick(d, "gradeStatus", "grade_status", "grade", "status", default="")).upper()
    if raw not in GradeStatus.__members__:
        raise GameMasterProtocolError("Grade response requires an explicit known status")
    return GradeResult(
        item_id=item_id,
        submission_id=submission_id,
        status=GradeStatus(raw) if raw in GradeStatus.__members__ else GradeStatus.PENDING,
        score=_pick(d, "officialScore", "score", "points"),
        raw=dict(d),
    )

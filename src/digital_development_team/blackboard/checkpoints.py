"""Checkpoint: work/<issue>/checkpoint.json (atomic) + bảng board (dashboard đọc).

Runner ghi 2 lần mỗi vòng: stage_done (trước transition) và ready (sau). Crash → resume không chạy lại stage.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from digital_development_team.core.enums import IssueStatus
from digital_development_team.core.errors import CheckpointError
from digital_development_team.core.models import Checkpoint
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import read_text, write_text_atomic


@dataclass(frozen=True, slots=True)
class BoardRow:
    issue_id: str
    status: IssueStatus
    stage: str
    tier: str
    rework: int
    attempt: int
    title: str | None
    updated_at: str


class CheckpointStore:
    def __init__(self, layout: HomeLayout, db: Database) -> None:
        self._layout = layout
        self._db = db

    def save(self, cp: Checkpoint, *, title: str | None = None) -> None:
        path = self._layout.checkpoint_path(cp.issue_id)
        write_text_atomic(path, cp.model_dump_json(indent=2, exclude_none=True) + "\n")
        self.upsert_board(cp, title=title)

    def load(self, issue: str) -> Checkpoint | None:
        path = self._layout.checkpoint_path(issue)
        if not path.is_file():
            return None
        try:
            return Checkpoint.model_validate_json(read_text(path))
        except OSError as e:
            raise CheckpointError(f"Cannot read checkpoint after bounded retries: {path}") from e
        except ValidationError as e:
            raise CheckpointError(
                f"{path} hỏng: {e.errors()[0]['msg']}", hint="ddt replay <issue> --resume dựng lại từ events"
            ) from e

    def exists(self, issue: str) -> bool:
        return self._layout.checkpoint_path(issue).is_file()

    def upsert_board(self, cp: Checkpoint, *, title: str | None = None) -> None:
        with self._db.transaction() as c:
            c.execute(
                "INSERT INTO board(issue_id, status, stage, tier, rework, attempt, title, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(issue_id) DO UPDATE SET "
                "status=excluded.status, stage=excluded.stage, tier=excluded.tier, rework=excluded.rework, "
                "attempt=excluded.attempt, title=COALESCE(excluded.title, board.title), updated_at=excluded.updated_at",
                (
                    cp.issue_id,
                    cp.status.value,
                    cp.stage.value,
                    cp.tier.value,
                    cp.rework,
                    cp.attempt,
                    title,
                    cp.saved_at.isoformat(),
                ),
            )

    def board(self, *, status: IssueStatus | None = None) -> list[BoardRow]:
        sql = "SELECT * FROM board"
        params: tuple[str, ...] = ()
        if status is not None:
            sql += " WHERE status=?"
            params = (status.value,)
        rows = self._db.all(sql + " ORDER BY issue_id", params)
        return [
            BoardRow(
                issue_id=r["issue_id"],
                status=IssueStatus(r["status"]),
                stage=r["stage"],
                tier=r["tier"],
                rework=r["rework"],
                attempt=r["attempt"],
                title=r["title"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

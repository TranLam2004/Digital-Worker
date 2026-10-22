"""Cổng Game Master — harness gọi, agent KHÔNG BAO GIỜ gọi (luật: cấm Admin/Grader API, cấm dò đề/MVS chưa mở).

Hai hiện thực: HttpGameMaster (server thật, đường dẫn theo policy.yaml#gamemaster.paths) · FakeGameMaster (luyện tập,
chạy kịch bản testdata/matches/*.json theo đúng luật v3.5).
"""

from __future__ import annotations

from typing import Any, Protocol

from digital_development_team.gamemaster.models import GradeResult, MatchSnapshot, StartResult, SubmitResult


class GameMaster(Protocol):
    @property
    def match_id(self) -> str: ...

    def snapshot(self) -> MatchSnapshot:
        """Trang /match/<id>: trạng thái trận, brief, backlog đã hiện (KHÔNG có item chưa reveal)."""
        ...

    def start_working(self, item_id: str) -> StartResult:
        """Báo bắt đầu làm item; nhận submit template (+ MVS nếu CR/BUG). Gọi lại: trả cùng thông tin."""
        ...

    def submit(
        self, item_id: str, *, git_revision: str, answer: dict[str, Any], idempotency_key: str
    ) -> SubmitResult:
        """Nộp 1 lần/item. gitRevision ở root body (không trong answer). Retry cùng key + body → bài cũ."""
        ...

    def submission(self, item_id: str, submission_id: str) -> GradeResult:
        """Kết quả chấm: PENDING · PASSED · FAILED."""
        ...

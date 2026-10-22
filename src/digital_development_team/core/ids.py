"""Kiểu định danh có ràng buộc. Dùng Annotated để Pydantic validate, vẫn là str lúc runtime."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

ISSUE_ID_RE = re.compile(r"^DDT-\d{4,}$")
SLOT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.(yaml|json)|/)$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

IssueId = Annotated[str, StringConstraints(pattern=ISSUE_ID_RE.pattern)]
"""DDT-0001, DDT-0042 …"""

SlotName = Annotated[str, StringConstraints(pattern=SLOT_NAME_RE.pattern)]
"""intake.yaml · spec.yaml · evidence.json · patch/ …"""

Role = Annotated[str, StringConstraints(pattern=ROLE_RE.pattern, to_lower=True)]
"""intake · planner · analyst · dev · verifier · security · leader · learner.
Là str có validate, KHÔNG phải Enum — thêm agent = thêm file .agent.md, không sửa code."""


def _posix_relative(value: str) -> str:
    if not value or value.startswith(("/", "\\")) or "\\" in value or ":" in value:
        raise ValueError(f"RepoPath phải là đường dẫn POSIX tương đối, không có ':' hay '\\': {value!r}")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"RepoPath không được chứa '.', '..' hay '//': {value!r}")
    return value


RepoPath = Annotated[str, AfterValidator(_posix_relative)]
"""Đường dẫn tương đối trong repo, luôn dạng POSIX: src/api/users.py. Chỉ đổi sang Path ở ranh giới fs."""


def is_issue_id(value: str) -> bool:
    return bool(ISSUE_ID_RE.match(value))


def next_issue_id(last: str | None) -> str:
    """DDT-0041 → DDT-0042. None → DDT-0001."""
    if last is None:
        return "DDT-0001"
    if not is_issue_id(last):
        raise ValueError(f"không phải IssueId: {last!r}")
    n = int(last.split("-", 1)[1]) + 1
    return f"DDT-{n:04d}"

"""WorktreeManager — một worktree/issue tại <home>/worktrees/<issue>, branch ddt/<issue> (10-harness/07).

Tạo khi stage cần fs (IMPLEMENT) — không tạo sớm ở INGEST/TRIAGE. BLOCKED → giữ (kept) cho người xem. DONE → remove (B12 merge).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from digital_development_team.core.errors import IsolationError
from digital_development_team.isolation.git import Git
from digital_development_team.storage.layout import HomeLayout

REPO_LOCK = threading.RLock()
"""Thao tác ghi lên repo chính (worktree add/remove, branch, merge ff, push) — nối tiếp khi nhiều issue chạy song
song trong một process (tránh git index.lock / ref lock). MERGE dùng chung khoá này."""


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    issue_id: str
    path: Path
    branch: str
    base_commit: str
    created: bool


class WorktreeManager:
    def __init__(self, git: Git, layout: HomeLayout) -> None:
        self._git = git
        self._layout = layout

    @property
    def git(self) -> Git:
        return self._git

    @staticmethod
    def branch(issue: str) -> str:
        return f"ddt/{issue}"

    def path(self, issue: str) -> Path:
        return self._layout.worktree_dir(issue)

    def exists(self, issue: str) -> bool:
        p = self.path(issue)
        return p.is_dir() and (p / ".git").exists()

    def default_branch(self) -> str:
        return self._git.default_branch()

    def ensure(self, issue: str, *, base_commit: str | None = None) -> WorktreeInfo:
        """Có rồi → dùng lại (base_commit từ checkpoint). Chưa → tạo từ HEAD default branch."""
        path = self.path(issue)
        branch = self.branch(issue)
        if self.exists(issue):
            base = base_commit or self._git.merge_base(self.default_branch(), "HEAD", cwd=path)
            return WorktreeInfo(issue, path, branch, base, created=False)
        if path.exists():
            raise IsolationError(
                f"{path} tồn tại nhưng không phải worktree", hint="xoá thư mục hoặc git worktree prune"
            )
        with REPO_LOCK:
            default = self.default_branch()
            base = self._git.rev_parse(default)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._git.worktree_add(path, branch=branch, base=base)
        return WorktreeInfo(issue, path, branch, base, created=True)

    def remove(self, issue: str, *, force: bool = False, delete_branch: bool = True) -> bool:
        if not self.exists(issue):
            return False
        with REPO_LOCK:
            self._git.worktree_remove(self.path(issue), force=force)
            if delete_branch:
                self._git.branch_delete(self.branch(issue))
        return True

    def reset_onto(self, issue: str, onto: str, *, backup_suffix: str) -> tuple[str, str]:
        """Merge conflict: giữ bản cũ ở nhánh backup `ddt/<issue>-<suffix>`, đưa worktree về đúng `onto`
        (base mới) để lần IMPLEMENT kế làm lại trên base mới. Trả (sha base mới, tên nhánh backup)."""
        path = self.path(issue)
        backup = f"{self.branch(issue)}-{backup_suffix}"
        if self._git.is_dirty(cwd=path):
            self._git.commit(cwd=path, message=f"ddt({issue}): wip trước khi reset lên base mới")
        with REPO_LOCK:
            self._git.branch_force(backup, "HEAD", cwd=path)
        new_base = self._git.rev_parse(onto)
        self._git.reset_hard(new_base, cwd=path)
        return new_base, backup

    def is_dirty(self, issue: str) -> bool:
        return self.exists(issue) and self._git.is_dirty(cwd=self.path(issue))

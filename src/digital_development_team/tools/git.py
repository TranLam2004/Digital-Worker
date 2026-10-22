"""git.* cho agent — diff · log · status · commit. Không có push. Chạy trong worktree của issue."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from digital_development_team.core.context import ToolContext
from digital_development_team.core.errors import IsolationError
from digital_development_team.core.models import ToolResult
from digital_development_team.isolation.git import Git
from digital_development_team.tools.base import Handler


class GitTools:
    def __init__(self, git: Git, *, base_ref: Callable[[ToolContext], str] | None = None) -> None:
        self._git = git
        self._base_ref = base_ref or (lambda ctx: "main")

    def handlers(self) -> dict[str, Handler]:
        return {"diff": self.diff, "log": self.log, "status": self.status, "commit": self.commit}

    def _wt(self, ctx: ToolContext) -> Path | ToolResult:
        if not ctx.worktree:
            return ToolResult.fail("NO_WORKTREE", "stage này không có worktree")
        return Path(ctx.worktree)

    def diff(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        wt = self._wt(ctx)
        if isinstance(wt, ToolResult):
            return wt
        stat_only = bool(args.get("stat", False)) or ctx.actor.role == "leader"
        path = args.get("path")
        base = self._base_ref(ctx)
        try:
            diff = self._git.diff_worktree(cwd=wt, base=base)
            if path:
                diff = _filter_diff(diff, str(path))
            files = _stat(diff)
        except IsolationError as e:
            return ToolResult.fail("GIT", e.message)
        added = sum(f["added"] for f in files)
        removed = sum(f["removed"] for f in files)
        return ToolResult.ok(
            f"diff {base}…worktree → {len(files)} files, +{added} -{removed}",
            files=files,
            total_added=added,
            total_removed=removed,
            diff="" if stat_only else diff,
            base=base,
        )

    def log(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        wt = self._wt(ctx)
        if isinstance(wt, ToolResult):
            return wt
        try:
            commits = self._git.log(
                cwd=wt, n=int(args.get("n") or 20), path=args.get("path"), search=args.get("search")
            )
        except IsolationError as e:
            return ToolResult.fail("GIT", e.message)
        return ToolResult.ok(f"log → {len(commits)} commits", commits=commits)

    def status(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        wt = self._wt(ctx)
        if isinstance(wt, ToolResult):
            return wt
        try:
            branch = self._git.current_branch(cwd=wt)
            entries = self._git.status_porcelain(cwd=wt)
        except IsolationError as e:
            return ToolResult.fail("GIT", e.message)
        staged = [p for xy, p in entries if xy[0] not in (" ", "?")]
        modified = [p for xy, p in entries if xy[1] == "M"]
        untracked = [p for xy, p in entries if xy == "??"]
        return ToolResult.ok(
            f"status {branch} → {len(modified)} modified, {len(untracked)} untracked, {len(staged)} staged",
            branch=branch,
            modified=modified,
            untracked=untracked,
            staged=staged,
        )

    def commit(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        wt = self._wt(ctx)
        if isinstance(wt, ToolResult):
            return wt
        message = str(args.get("message", "")).strip()
        prefix = f"ddt({ctx.issue_id}): "
        if not message.startswith(prefix):
            message = prefix + message
        try:
            # double-check scope: file ngoài allowed_files → không commit
            if ctx.allowed_files:
                from digital_development_team.util.globs import GlobSet
                from digital_development_team.util.paths import normcase

                gs = GlobSet(ctx.allowed_files)
                allowed_norm = {normcase(a) for a in ctx.allowed_files}
                outside = [
                    p
                    for _, p in self._git.status_porcelain(cwd=wt)
                    if not gs.match(p) and normcase(p) not in allowed_norm
                ]
                if outside:
                    return ToolResult.fail(
                        "SCOPE",
                        f"file ngoài allowed_files: {outside}",
                        hint="Hoàn tác thay đổi ngoài scope trước khi commit",
                        outside=outside,
                    )
            sha = self._git.commit(cwd=wt, message=message, all_=bool(args.get("all", True)))
        except IsolationError as e:
            return ToolResult.fail("GIT", e.message)
        if sha is None:
            return ToolResult.fail(
                "NOTHING_TO_COMMIT",
                "không có thay đổi",
                hint="Chưa sửa gì? Kiểm tra fs.edit có thành công không",
            )
        stats = _stat(self._git.diff(cwd=wt, base=f"{sha}~1"))
        return ToolResult.ok(
            f"commit {sha[:7]} → {len(stats)} files",
            sha=sha[:12],
            files=len(stats),
            insertions=sum(s["added"] for s in stats),
            deletions=sum(s["removed"] for s in stats),
        )


def _stat(diff: str) -> list[dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    cur: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:].strip()
            files.setdefault(cur, {"path": cur, "added": 0, "removed": 0, "hunks": 0})
        elif line.startswith("+++ /dev/null"):
            cur = None
        elif cur and line.startswith("@@"):
            files[cur]["hunks"] += 1
        elif cur and line.startswith("+") and not line.startswith("+++"):
            files[cur]["added"] += 1
        elif cur and line.startswith("-") and not line.startswith("---"):
            files[cur]["removed"] += 1
    return list(files.values())


def _filter_diff(diff: str, path: str) -> str:
    out: list[str] = []
    keep = False
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            keep = f" b/{path}" in line
        if keep:
            out.append(line)
    return "".join(out)

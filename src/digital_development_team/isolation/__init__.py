"""Isolation — git worktree per issue · merge tuần tự · lock (10-harness/07).

    git = Git(repo_root, scratch=home/state/tmp)
    git.worktree_add(path, branch="ddt/DDT-0001", base="main")
    git.diff(cwd=wt, base="main") · git.commit(cwd=wt, message=...) · git.rebase(cwd=wt, onto="main") · git.merge_ff_only("ddt/DDT-0001")

Chỉ isolation/git.py (và util/proc.py) được gọi subprocess. worktree.py/merge.py: B9/B12. locks.py: P2#21.
"""

from __future__ import annotations

from digital_development_team.isolation.git import Git, GitResult

__all__ = ["Git", "GitResult"]

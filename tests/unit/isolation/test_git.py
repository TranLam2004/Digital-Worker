from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from digital_development_team.core.errors import IsolationError
from digital_development_team.isolation import Git
from tests.conftest import TESTDATA

MINI = TESTDATA / "repos" / "mini_calc"


def make_repo(tmp_path: Path) -> tuple[Git, Path]:
    """Copy mini_calc → git init → commit đầu trên main."""
    repo = tmp_path / "repo"
    shutil.copytree(MINI, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    g = Git(repo, scratch=scratch)
    g.init()
    g.commit(message="init mini_calc")
    return g, repo


def test_init_commit_worktree_diff_merge(tmp_path: Path) -> None:
    g, _ = make_repo(tmp_path)
    base = g.head()
    assert g.default_branch() == "main" and not g.is_dirty()
    wt = tmp_path / "wt" / "DDT-0001"
    g.worktree_add(wt, branch="ddt/DDT-0001", base="main")
    assert g.current_branch(cwd=wt) == "ddt/DDT-0001" and str(wt.resolve()).replace("\\", "/") in [
        p.replace("\\", "/") for p in g.worktree_list()
    ]
    calc = wt / "calc.py"
    calc.write_text(
        calc.read_text(encoding="utf-8").replace(
            "    return sum(xs) / len(xs)",
            "    if not xs:\n        raise ValueError('mean of empty list')\n    return sum(xs) / len(xs)",
        ),
        encoding="utf-8",
    )
    assert g.is_dirty(cwd=wt) and g.status_porcelain(cwd=wt) == [(" M", "calc.py")]
    d = g.diff_worktree(cwd=wt, base="main")
    assert "+++ b/calc.py" in d and "+        raise ValueError" in d
    sha = g.commit(cwd=wt, message="ddt(DDT-0001): guard empty")
    assert sha and sha != base and g.commit(cwd=wt, message="nothing") is None
    assert g.changed_files(cwd=wt, base="main") == ["calc.py"]
    assert g.numstat(cwd=wt, base="main") == [("calc.py", 2, 0)]
    log = g.log(cwd=wt, n=5)
    assert (
        log[0]["message"] == "ddt(DDT-0001): guard empty"
        and len(log[0]["author_hash"]) == 8
        and "@" not in str(log[0])
    )
    ok, files = g.rebase(cwd=wt, onto="main")
    assert ok and files == []
    new_head = g.merge_ff_only("ddt/DDT-0001")
    assert new_head == sha and g.head() == sha
    g.worktree_remove(wt)
    assert not wt.exists()
    g.branch_delete("ddt/DDT-0001")
    assert not g.branch_exists("ddt/DDT-0001")


def test_rebase_conflict_aborts(tmp_path: Path) -> None:
    g, repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    g.worktree_add(wt, branch="ddt/DDT-0002", base="main")
    (wt / "calc.py").write_text("# worktree version\n", encoding="utf-8")
    g.commit(cwd=wt, message="wt change")
    (repo / "calc.py").write_text("# main version\n", encoding="utf-8")
    g.commit(message="main change")
    ok, files = g.rebase(cwd=wt, onto="main")
    assert not ok and files == ["calc.py"]
    assert (
        not (wt / ".git" / "rebase-merge").exists() and g.current_branch(cwd=wt) == "ddt/DDT-0002"
    )  # đã abort
    with pytest.raises(IsolationError):
        g.merge_ff_only("ddt/DDT-0002")  # không ff được


def test_identity_and_errors(tmp_path: Path) -> None:
    g, _ = make_repo(tmp_path)
    show = g.run(["show", "-s", "--format=%an <%ae>", "HEAD"]).check("show").stdout.strip()
    assert show == "ddt-dev <ddt@local>"
    with pytest.raises(IsolationError, match="thất bại"):
        g.run(["rev-parse", "--verify", "nope"]).check("rev-parse nope")
    assert not g.branch_exists("nope")

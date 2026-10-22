from __future__ import annotations

from pathlib import Path

from digital_development_team.isolation.worktree import WorktreeManager
from digital_development_team.storage import HomeLayout
from tests.unit.isolation.test_git import make_repo


def test_worktree_lifecycle(tmp_path: Path) -> None:
    g, _ = make_repo(tmp_path)
    layout = HomeLayout(tmp_path / "home.ddt").ensure()
    wm = WorktreeManager(g, layout)
    assert not wm.exists("DDT-0001")
    info = wm.ensure("DDT-0001")
    assert info.created and info.branch == "ddt/DDT-0001" and info.path == layout.worktree_dir("DDT-0001")
    assert info.base_commit == g.head() and wm.exists("DDT-0001") and not wm.is_dirty("DDT-0001")
    again = wm.ensure("DDT-0001", base_commit=info.base_commit)
    assert not again.created and again.base_commit == info.base_commit
    (info.path / "calc.py").write_text("x\n", encoding="utf-8")
    assert wm.is_dirty("DDT-0001")
    assert wm.remove("DDT-0001", force=True)
    assert not wm.exists("DDT-0001") and not g.branch_exists("ddt/DDT-0001") and not wm.remove("DDT-0001")

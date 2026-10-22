from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from digital_development_team.cli.match import _check_team_repo
from digital_development_team.config.loader import load_config
from digital_development_team.core.errors import ConfigError, IsolationError
from digital_development_team.onboarding import onboard
from digital_development_team.sandbox import build_sandbox
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo


def test_team_preflight_checks_remote_but_resume_allows_pending_push(tmp_path: Path) -> None:
    git, repo = make_repo(tmp_path)
    (repo / "M-1").mkdir()
    (repo / "M-1/app.py").write_text("value = 1\n", encoding="utf-8")
    git.commit(message="match source")
    remote = tmp_path / "team.git"
    git.run(["init", "--bare", str(remote)]).check("init remote")
    git.run(["remote", "add", "origin", str(remote)]).check("add remote")
    git.push_branch("origin", "main")
    runtime = SimpleNamespace(repo=repo, config=load_config(), layout=SimpleNamespace(state_dir=tmp_path))
    assert _check_team_repo(runtime, "M-1") == "M-1"
    (repo / "M-1/app.py").write_text("value = 2\n", encoding="utf-8")
    git.commit(message="local unpushed result")
    with pytest.raises(IsolationError):
        _check_team_repo(runtime, "M-1")
    assert _check_team_repo(runtime, "M-1", verify_remote=False) == "M-1"
    with pytest.raises(ConfigError, match="folder"):
        _check_team_repo(runtime, "M-missing", verify_remote=False)


def test_onboarding_runs_only_current_match_project(tmp_path: Path) -> None:
    git, repo = make_repo(tmp_path)
    shutil.copytree(
        TESTDATA / "repos/mini_calc",
        repo / "M-1",
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    (repo / "M-2").mkdir()
    (repo / "M-2/package.json").write_text('{"scripts":{"test":"exit 1"}}', encoding="utf-8")
    git.commit(message="two matches")
    cfg = load_config().policy.sandbox
    scratch = tmp_path / "onboard-tmp"
    scratch.mkdir()
    report = onboard(
        repo,
        git=git,
        sandbox=build_sandbox(cfg, scratch=scratch),
        sandbox_cfg=cfg,
        memory_dir=tmp_path / "memory",
        scratch=scratch,
        scope_dir="M-1",
        timeout_s=30,
    )
    assert report.stacks and all(s.root == "M-1" for s in report.stacks)
    assert report.chosen.get("test", "").startswith("cd M-1 && ")
    assert not git.is_dirty() and len(git.worktree_list()) == 1

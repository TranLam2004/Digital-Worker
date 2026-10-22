"""Phase 6: ddt onboard — nhận diện stack, probe lệnh trong worktree tạm, ghi REPO.md (giữ phần người viết)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from digital_development_team.config.loader import load_config
from digital_development_team.isolation.git import Git
from digital_development_team.onboarding import OnboardReport, merge_repo_md, onboard
from digital_development_team.onboarding.detect import detect_stacks
from digital_development_team.runner.stages.machine_gate import repo_commands
from digital_development_team.sandbox import build_sandbox
from tests.conftest import TESTDATA


def _stacks(files: dict[str, str]) -> dict[str, dict[str, list[str]]]:
    return {f"{s.language}:{s.root}": s.commands for s in detect_stacks(list(files), files.get)}


def test_detect_stacks_per_language() -> None:
    pkg = json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}, "devDependencies": {"jest": "29"}})
    got = _stacks(
        {
            "web/package.json": pkg,
            "web/yarn.lock": "",
            "web/tsconfig.json": "{}",
            "api/go.mod": "module x",
            "svc/pom.xml": "<project/>",
            "pytest.ini": "",
            "tests/test_a.py": "",
            "Lib/Lib.csproj": "",
            "Lib.Tests/Lib.Tests.csproj": "",
        }
    )
    assert got["javascript:web"]["test"] == ["cd web && yarn test"]
    assert got["javascript:web"]["typecheck"] == ["cd web && npx --no-install tsc --noEmit"]
    assert got["go:api"]["lint"] == ["cd api && go vet ./..."]
    assert got["java:svc"]["test"][0] == "cd svc && mvn -q -o test"
    assert got["python:"]["test"] == ["python -m pytest -q"]
    assert got["dotnet:"]["test"] == ["dotnet test Lib.Tests/Lib.Tests.csproj"]


def test_merge_keeps_human_sections() -> None:
    human = "# REPO.md\n\n## Overview\nhand written\n\n## Build & test\n| Build | `old` |\n\n## Conventions\nkeep me\n"
    rep = OnboardReport(stacks=[], missing={}, chosen={"build": "go build ./...", "test": "go test ./..."})
    out = merge_repo_md(human, rep)
    assert "hand written" in out and "keep me" in out and "`old`" not in out
    assert repo_commands(out) == {"build": "go build ./..."}  # machine gate đọc được bảng mới


@pytest.mark.skipif(shutil.which("go") is None, reason="cần Go")
def test_onboard_go_repo_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "shop_go"
    shutil.copytree(TESTDATA / "repos" / "shop_go", repo)
    (repo / "REPO.md").unlink()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    git = Git(repo, scratch=scratch)
    git.init()
    git.commit(message="init")
    cfg = load_config().policy.sandbox
    rep = onboard(
        repo,
        git=git,
        sandbox=build_sandbox(cfg, scratch=scratch),
        sandbox_cfg=cfg,
        memory_dir=tmp_path / "memory",
        scratch=scratch,
        timeout_s=300,
    )
    assert [s.language for s in rep.stacks] == ["go"] and not rep.missing
    assert rep.chosen == {"build": "go build ./...", "lint": "go vet ./...", "test": "go test ./..."}
    assert repo_commands((tmp_path / "memory" / "REPO.md").read_text(encoding="utf-8")) == {
        "build": "go build ./...",
        "lint": "go vet ./...",
    }
    assert (
        not git.is_dirty() and len(git.worktree_list()) == 1
    )  # repo người dùng không đổi, worktree tạm đã xoá

from __future__ import annotations

from pathlib import Path

from digital_development_team.runner.stages.preflight import _inspect


def test_match_subdirectory_resolves_existing_and_planned_tests(tmp_path: Path) -> None:
    project = tmp_path / "M-1"
    (project / "tests").mkdir(parents=True)
    (project / "tests/test_old.py").write_text("", encoding="utf-8")
    problems, env = _inspect(
        "cd M-1 && python -m pytest tests/test_old.py tests/test_new.py -q",
        tmp_path,
        ["M-1/tests/test_new.py"],
    )
    assert not problems and not env
    problems, _ = _inspect("cd M-1 && python -m pytest tests/test_missing.py -q", tmp_path, [])
    assert problems


def test_preflight_cd_cannot_escape_worktree(tmp_path: Path) -> None:
    problems, _ = _inspect("cd .. && python -m pytest -q", tmp_path, ["**"])
    assert problems

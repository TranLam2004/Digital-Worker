"""repo_commands: REPO.md dạng list `- lint_command:` và dạng bảng `| Lint | \\`cmd\\` |` (template 50-knowledge) đều đọc được."""

from __future__ import annotations

from digital_development_team.runner.stages.machine_gate import repo_commands

TABLE = """## Build & test

| Việc | Lệnh | Thời gian | Ghi chú |
|---|---|---|---|
| Install | `pip install -r requirements.txt` | 45s | |
| Build | `—` hoặc `npm run build` | | |
| Test full | `pytest tests/ -q` | 3m | |
| Lint | `ruff check src/` | 2s | |
| Typecheck | `mypy src/` | | |
"""
LIST = """## Build & test
- lint_command: `ruff check .`
- typecheck_command: mypy .
"""


def test_table_rows() -> None:
    cmds = repo_commands(TABLE)
    assert cmds == {"lint": "ruff check src/", "typecheck": "mypy src/"}  # Build `—` = không khai báo


def test_list_lines_win_over_table() -> None:
    cmds = repo_commands(TABLE + "\n" + LIST)
    assert cmds["lint"] == "ruff check ." and cmds["typecheck"] == "mypy ."
    assert repo_commands(None) == {} and repo_commands("# nothing") == {}

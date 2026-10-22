"""MCP server DDT (ddt mcp --session): expose đúng tool của role qua Dispatcher — cùng policy, cùng allowlist với transport API."""

from __future__ import annotations

import json
from pathlib import Path

from digital_development_team.app import build_runtime
from digital_development_team.cli.mcp_serve import build_handlers
from digital_development_team.llm.claude_cli_provider import CliSession
from tests.unit.isolation.test_git import make_repo


def _session(home: Path, repo: Path, role: str = "dev") -> CliSession:
    return CliSession(
        home=str(home),
        repo=str(repo),
        config_dir=None,
        issue_id="DDT-0001",
        role=role,
        stage="IMPLEMENT",
        tier="T1",
        worktree=str(repo),
        allowed_files=("calc.py", "tests/test_calc.py"),
        spec_values={"spec.test_command": "pytest -q"},
        autonomy="balanced",
        tools=(),
    )


def test_list_and_call_tools_go_through_dispatcher(tmp_home: Path, tmp_path: Path) -> None:
    _, repo = make_repo(tmp_path)
    build_runtime(tmp_home, repo=repo, provider="mock").close()  # tạo home/db
    list_tools, call_tool = build_handlers(_session(tmp_home, repo))
    names = {t["name"] for t in list_tools()}
    assert {"fs_read", "fs_edit", "blackboard_write", "test_run", "policy"} <= names
    assert "git_push" not in names and "fs_delete" not in names  # dev không có
    assert all(isinstance(t["inputSchema"], dict) for t in list_tools())

    # permission prompt tool: luôn allow (lớp chặn thật là dispatch)
    d = json.loads(call_tool("policy", {"tool_name": "mcp__ddt__fs_read", "input": {"path": "calc.py"}}))
    assert d["behavior"] == "allow" and d["updatedInput"] == {"path": "calc.py"}

    text = call_tool("fs_read", {"path": "calc.py"})
    assert "def mean" in text

    # ngoài worktree → policy deny (không exception)
    denied = call_tool("fs_read", {"path": "../outside.txt"})
    assert "denied" in denied.lower() or "error" in denied.lower()

    unknown = call_tool("nope", {})
    assert "UNKNOWN_TOOL" in unknown


def test_readonly_role_cannot_edit(tmp_home: Path, tmp_path: Path) -> None:
    _, repo = make_repo(tmp_path)
    build_runtime(tmp_home, repo=repo, provider="mock").close()
    list_tools, call_tool = build_handlers(_session(tmp_home, repo, role="verifier"))
    names = {t["name"] for t in list_tools()}
    assert "fs_edit" not in names and "fs_read" in names and "test_run" in names
    res = call_tool("fs_edit", {"path": "calc.py", "old": "x", "new": "y"})
    assert "ok" not in res[:20].lower()

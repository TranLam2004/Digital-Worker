"""Transport Codex CLI: argv cô lập · TOML cho -c · parse `--json` · budget kill · rate limit · chưa đăng nhập · preset.

Không gọi `codex` thật — binary giả tests/unit/llm/fake_codex.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from digital_development_team.config.loader import load_config
from digital_development_team.config.models import CodexCfg, ProviderCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import ProviderError, RateLimited
from digital_development_team.core.llm import ChatRequest, Message, ModelRef, StopReason, Usage
from digital_development_team.llm.cli_session import CliAgentResult, CliSession, TurnInfo
from digital_development_team.llm.codex_cli_provider import (
    CodexCliProvider,
    CodexStreamParser,
    looks_rate_limited,
    resolve_binary,
    toml_value,
    usage_from,
)
from digital_development_team.llm.registry import ModelRegistry

FAKE = Path(__file__).with_name("fake_codex.py")
REF = ModelRef(provider="codex_cli", model="gpt-fake", family="gpt", alias="sonnet", effort="medium")


def _cfg(**codex: object) -> ProviderCfg:
    return ProviderCfg(
        adapter="codex_cli",
        binary=f"{sys.executable} {FAKE}",
        auth="subscription",
        supports=["agent_loop", "tool_use"],
        enabled=True,
        codex=CodexCfg(**codex),  # type: ignore[arg-type]
    )


def _provider(tmp_path: Path, **codex: object) -> CodexCliProvider:
    return CodexCliProvider(_cfg(**codex), scratch=tmp_path / "scratch")


def _session(tmp_path: Path) -> CliSession:
    return CliSession(
        home=str(tmp_path / "home"),
        repo=None,
        config_dir=None,
        issue_id="DDT-0001",
        role="dev",
        stage="IMPLEMENT",
        tier="T1",
        worktree=str(tmp_path),
        allowed_files=("calc.py",),
        spec_values={"spec.test_command": "pytest -q"},
        autonomy="balanced",
        tools=("fs.read", "blackboard.write"),
    )


def _overrides(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(argv):
        if a == "-c":
            k, v = argv[i + 1].split("=", 1)
            out[k] = v
    return out


# ───── config ─────
def test_codex_cfg_requires_shell_off_and_no_project_doc() -> None:
    with pytest.raises(ValueError, match="shell_tool"):
        CodexCfg(disable_features=["apps"])
    with pytest.raises(ValueError, match="project_doc_max_bytes"):
        CodexCfg(project_doc_max_bytes=1000)
    with pytest.raises(ValueError, match="block codex"):
        ProviderCfg(adapter="codex_cli", enabled=True)
    assert ProviderCfg(adapter="codex_cli", codex=CodexCfg()).is_cli
    assert not ProviderCfg(adapter="openai").is_cli


def test_toml_value_quotes_windows_paths_and_tables() -> None:
    assert toml_value("C:\\a b\\py.exe") == '"C:\\\\a b\\\\py.exe"'
    assert toml_value(["-m", "x"]) == '["-m", "x"]'
    assert toml_value({"PATH": "p", "A-B": "1"}) == '{PATH = "p", A-B = "1"}'
    assert toml_value(False) == "false" and toml_value(60) == "60"


def test_resolve_binary_prefers_native_exe_next_to_npm_wrapper(tmp_path: Path) -> None:
    cmd = tmp_path / "codex.cmd"
    cmd.write_text("@echo off", encoding="utf-8")
    native = (
        tmp_path
        / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin"
    )
    native.mkdir(parents=True)
    (native / "codex.exe").write_bytes(b"")
    assert resolve_binary(str(cmd)) == str(native / "codex.exe")
    assert resolve_binary(sys.executable) == sys.executable


# ───── argv ─────
def test_agent_argv_isolated_and_tools_through_mcp(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    argv = p.agent_argv(
        model="gpt-x",
        system_file=tmp_path / "sys.md",
        cwd=tmp_path,
        session_path=tmp_path / "s.json",
        effort="low",
    )
    assert argv[2:4] == ["exec", "--json"] and argv[-1] == "-"  # prompt qua stdin
    for flag in ("--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"):
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "read-only" and argv[argv.index("-m") + 1] == "gpt-x"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
    assert "shell_tool" in disabled and "multi_agent" in disabled
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    ov = _overrides(argv)
    assert ov["approval_policy"] == '"never"' and ov["project_doc_max_bytes"] == "0"
    assert ov["web_search"] == '"disabled"' and ov["model_reasoning_effort"] == '"low"'
    assert json.loads(ov["model_instructions_file"]).endswith("sys.md")
    assert json.loads(ov["mcp_servers.ddt.command"]) == sys.executable
    assert ov["mcp_servers.ddt.args"].startswith(
        '["-m", "digital_development_team.cli.mcp_serve", "--session"'
    )
    assert ov["mcp_servers.ddt.tool_timeout_sec"] == "600"
    assert "PYTHONUTF8" in ov["mcp_servers.ddt.env"]


def test_extra_config_and_codex_home(tmp_path: Path) -> None:
    p = _provider(tmp_path, extra_config={"model_verbosity": "low"}, codex_home=str(tmp_path / "ch"))
    argv = p.base_argv(model="m", system_file=tmp_path / "s.md", cwd=tmp_path)
    assert _overrides(argv)["model_verbosity"] == '"low"'
    assert p._env()["CODEX_HOME"] == str(tmp_path / "ch")


# ───── parse ─────
def test_usage_openai_input_includes_cached() -> None:
    assert usage_from({"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 9}) == Usage(
        tokens_in=100, tokens_out=9, tokens_cached=40
    )


def test_parser_one_turn_per_tool_call_and_usage_at_end() -> None:
    r = CliAgentResult()
    seen: list[TurnInfo] = []
    p = CodexStreamParser(r, lambda t: seen.append(t) or True, "m")
    p.feed(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    p.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "look"}}))
    p.feed(
        json.dumps(
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "ddt", "tool": "fs_read"}}
        )
    )
    p.feed("not json")
    p.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
    p.feed(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}))
    assert [t.tool_calls for t in seen] == [("fs_read",), ()]
    assert seen[0].text == "look" and seen[0].usage == Usage() and seen[1].usage.tokens_in == 10
    assert r.turns == 2 and r.final_text == "done" and r.usage.tokens_out == 2 and p.thread_id == "t1"


def test_parser_turn_failed_and_rate_limit() -> None:
    r = CliAgentResult()
    p = CodexStreamParser(r, lambda t: True)
    p.feed(json.dumps({"type": "error", "message": "Reconnecting... 1/5"}))
    assert not r.is_error
    p.feed(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}))
    assert r.is_error and r.rate_limited and "usage limit" in r.stderr_tail


def test_rate_limit_patterns_ignore_hex_ids() -> None:
    assert looks_rate_limited("HTTP 429 Too Many Requests") and looks_rate_limited("insufficient_quota")
    assert not looks_rate_limited("cf-ray: a4b60b429ea85b5-HKG") and not looks_rate_limited("ok")


# ───── Complete ─────
def test_chat_via_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "chat")
    argv_file, stdin_file = tmp_path / "argv.json", tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_CODEX_ARGV", str(argv_file))
    monkeypatch.setenv("FAKE_CODEX_STDIN", str(stdin_file))
    p = _provider(tmp_path)
    resp = p.chat(
        ChatRequest(model=REF, system="SYS", messages=(Message.user_text("ping"),)), cancel=CancelToken()
    )
    assert resp.text == "echo:ping" and resp.stop is StopReason.END_TURN and resp.raw_id == "thread-1"
    assert resp.usage.tokens_in == 150 and resp.usage.tokens_cached == 50 and resp.usage.tokens_out == 7
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert "ping" not in argv and not any("mcp_servers" in a for a in argv)  # 1 call: không MCP
    assert stdin_file.read_text(encoding="utf-8") == "ping"
    assert not list((tmp_path / "scratch").glob("codex-system-*"))  # system file đã dọn


def test_chat_not_logged_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "unauth")
    with pytest.raises(ProviderError, match="chưa đăng nhập") as ei:
        _provider(tmp_path).chat(
            ChatRequest(model=REF, system="S", messages=(Message.user_text("x"),)), cancel=CancelToken()
        )
    assert not ei.value.retryable


def test_chat_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "limited")
    with pytest.raises(RateLimited):
        _provider(tmp_path).chat(
            ChatRequest(model=REF, system="S", messages=(Message.user_text("x"),)), cancel=CancelToken()
        )


# ───── RunAgent ─────
def _run(p: CodexCliProvider, tmp_path: Path, on_turn: object = None) -> CliAgentResult:
    return p.run_agent(
        session=_session(tmp_path),
        system="S",
        brief="B",
        model="gpt-fake",
        effort=None,
        cancel=CancelToken(),
        on_turn=on_turn or (lambda t: True),  # type: ignore[arg-type]
    )


def test_run_agent_streams_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "stream")
    turns: list[TurnInfo] = []
    res = _run(_provider(tmp_path), tmp_path, lambda t: turns.append(t) or True)
    assert [t.tool_calls for t in turns] == [("fs_read",), ("blackboard_write",), ()]
    assert res.turns == 3 and res.final_text == "done" and not res.is_error and not res.killed
    assert res.usage.tokens_in == 2400 and res.usage.tokens_cached == 400 and res.exit_code == 0
    sess = next((tmp_path / "scratch").glob("cli-session-*.json"))
    assert CliSession.load(sess).permission_tool is False  # MCP server ẩn tool `policy`


def test_run_agent_killed_when_harness_says_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "slow")
    res = _run(_provider(tmp_path), tmp_path, lambda t: t.index < 3)
    assert res.killed and 3 <= res.turns < 20


def test_run_agent_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "limited")
    res = _run(_provider(tmp_path), tmp_path)
    assert res.rate_limited and res.is_error and res.turns == 0


def test_run_agent_unauth_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "unauth")
    with pytest.raises(ProviderError, match="chưa đăng nhập"):
        _run(_provider(tmp_path), tmp_path)


def test_builtin_tool_leak_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "builtin")
    turns: list[TurnInfo] = []
    _run(_provider(tmp_path), tmp_path, lambda t: turns.append(t) or True)
    assert turns[0].tool_calls == ("codex.command_execution",)


# ───── preset ─────
class _Fake:
    name = "codex_cli"
    caps = CodexCliProvider.caps

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> object:  # pragma: no cover
        raise NotImplementedError


def test_codex_all_preset_routes_every_role_to_codex() -> None:
    cfg = load_config(preset="codex_all")
    reg = ModelRegistry(cfg.models, env={}, providers={"codex_cli": _Fake()})  # type: ignore[dict-item]
    for role in ("intake", "planner", "analyst", "dev", "verifier", "security", "leader"):
        assert reg.resolve(role).ref.provider == "codex_cli", role
    assert reg.transport_for("dev") == "cli"

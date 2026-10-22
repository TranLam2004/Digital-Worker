"""Transport CLI (Claude Code headless): argv an toàn · parse stream-json · budget kill · rate limit · fallback alias · cost 0.

Không gọi `claude` thật — binary giả tests/unit/llm/fake_claude.py. Live: tests/live/test_claude_cli_live.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from digital_development_team.config.loader import load_config
from digital_development_team.config.models import CliCfg, ProviderCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import ProviderError, RateLimited
from digital_development_team.core.llm import (
    ChatRequest,
    ChatResponse,
    Message,
    ModelRef,
    StopReason,
    TextBlock,
    Usage,
)
from digital_development_team.llm.claude_cli_provider import (
    ClaudeCliProvider,
    CliAgentResult,
    CliSession,
    StreamParser,
    TurnInfo,
    looks_rate_limited,
    parse_assistant_event,
    rate_limit_hit,
    split_binary,
)
from digital_development_team.llm.registry import ModelRegistry

FAKE = Path(__file__).with_name("fake_claude.py")


def _cfg(binary: str | None = None) -> ProviderCfg:
    return ProviderCfg(
        adapter="claude_cli",
        binary=binary or f"{sys.executable} {FAKE}",
        auth="subscription",
        supports=["agent_loop", "tool_use"],
        enabled=True,
        cli=CliCfg(),
    )


def _provider(tmp_path: Path, binary: str | None = None) -> ClaudeCliProvider:
    return ClaudeCliProvider(_cfg(binary), scratch=tmp_path / "scratch")


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


REF = ModelRef(
    provider="anthropic_cli", model="claude-fake", family="claude", alias="opus_cli", effort="medium"
)


# ───── config ─────
def test_cli_cfg_rejects_builtin_tools_and_project_settings() -> None:
    with pytest.raises(ValueError, match=r"cli.tools"):
        CliCfg(tools=["Bash"])
    with pytest.raises(ValueError, match=r"setting_sources"):
        CliCfg(setting_sources=["user", "project"])
    with pytest.raises(ValueError, match="block cli"):
        ProviderCfg(adapter="claude_cli", enabled=True)


def test_split_binary() -> None:
    assert split_binary(None) == ["claude"]
    assert split_binary('"C:/Program Files/claude.cmd" --x') == ["C:/Program Files/claude.cmd", "--x"]


# ───── argv ─────
def test_argv_safe_flags(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    base = p.base_argv(model="claude-x", system_file=tmp_path / "sys.md", output_format="json", effort="low")
    assert "-p" in base and "--no-session-persistence" in base and "--strict-mcp-config" in base
    assert base[base.index("--tools") + 1] == ""  # tắt built-in tool
    assert base[base.index("--setting-sources") + 1] == "user"
    assert base[base.index("--model") + 1] == "claude-x" and base[base.index("--effort") + 1] == "low"
    assert "--dangerously-skip-permissions" not in base and "--verbose" not in base
    assert "--system-prompt" not in base and base[base.index("--system-prompt-file") + 1].endswith("sys.md")
    ag = p.agent_argv(model="claude-x", system_file=tmp_path / "sys.md", mcp_config=tmp_path / "mcp.json")
    assert ag[ag.index("--output-format") + 1] == "stream-json" and "--verbose" in ag
    assert ag[ag.index("--permission-prompt-tool") + 1] == "mcp__ddt__policy"
    assert ag[ag.index("--mcp-config") + 1] == str(tmp_path / "mcp.json")


def test_mcp_config_points_to_ddt_mcp(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    path = p.write_mcp_config(tmp_path / "mcp.json", tmp_path / "s.json")
    d = json.loads(path.read_text(encoding="utf-8"))
    srv = d["mcpServers"]["ddt"]
    assert srv["command"] == sys.executable and srv["args"][:3] == [
        "-m",
        "digital_development_team.cli.mcp_serve",
        "--session",
    ]


def test_session_roundtrip(tmp_path: Path) -> None:
    s = _session(tmp_path)
    s.dump(tmp_path / "s.json")
    assert CliSession.load(tmp_path / "s.json") == s


# ───── parse ─────
def test_parse_assistant_event_normalizes_usage() -> None:
    d = {
        "type": "assistant",
        "message": {
            "model": "m",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "name": "mcp__ddt__fs_read", "input": {}},
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
            },
        },
    }
    t = parse_assistant_event(d, 1)
    assert t.text == "hi" and t.tool_calls == ("mcp__ddt__fs_read",) and t.stop_reason == "tool_use"
    assert t.usage == Usage(tokens_in=130, tokens_out=5, tokens_cached=100)


def test_stream_parser_merges_blocks_of_same_message() -> None:
    def line(mid: str, block: dict[str, object], out: int, stop: str | None) -> str:
        return json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": mid,
                    "model": "m",
                    "content": [block],
                    "stop_reason": stop,
                    "usage": {"input_tokens": 100, "output_tokens": out},
                },
            }
        )

    r = CliAgentResult()
    seen: list[TurnInfo] = []
    p = StreamParser(r, lambda t: seen.append(t) or True)
    p.feed(line("A", {"type": "text", "text": "x"}, 3, None))
    p.feed(line("A", {"type": "tool_use", "name": "mcp__ddt__fs_read"}, 9, "tool_use"))
    assert seen == []  # chưa chốt — chờ id đổi
    p.feed(line("B", {"type": "text", "text": "done"}, 2, "end_turn"))
    assert (
        len(seen) == 1
        and seen[0].text == "x"
        and seen[0].tool_calls == ("mcp__ddt__fs_read",)
        and seen[0].usage.tokens_out == 9
    )
    p.feed(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "num_turns": 2,
                "usage": {"input_tokens": 200, "output_tokens": 11},
            }
        )
    )
    assert len(seen) == 2 and r.turns == 2 and r.usage.tokens_out == 11 and r.final_text == "done"


def test_rate_limit_detection() -> None:
    assert rate_limit_hit({"rate_limit_info": {"status": "rejected"}})
    assert not rate_limit_hit({"rate_limit_info": {"status": "allowed"}}) and not rate_limit_hit({})
    assert (
        looks_rate_limited("You've hit your usage limit")
        and looks_rate_limited("HTTP 429")
        and not looks_rate_limited("ok")
    )


def test_render_prompt_keeps_brief_first() -> None:
    req = ChatRequest(
        model=REF,
        system="S",
        messages=(
            Message.user_text("BRIEF"),
            Message(role="assistant", blocks=(TextBlock(text="A1"),)),
            Message.user_text("U2"),
        ),
    )
    text = ClaudeCliProvider.render_prompt(req)
    assert text.startswith("BRIEF") and "ASSISTANT (trước đó):\nA1" in text and text.endswith("USER:\nU2")


# ───── Complete (json) ─────
def test_chat_complete_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "json")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(argv_file))
    p = _provider(tmp_path)
    req = ChatRequest(model=REF, system="SYS", messages=(Message.user_text("ping"),))
    resp = p.chat(req, cancel=CancelToken())
    assert isinstance(resp, ChatResponse) and resp.text == "echo:ping" and resp.stop is StopReason.END_TURN
    assert resp.usage.tokens_in == 150 and resp.usage.tokens_cached == 50 and resp.usage.tokens_out == 7
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert "ping" not in argv and argv[argv.index("--output-format") + 1] == "json"  # prompt qua stdin


def test_chat_not_logged_in_maps_to_provider_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    p = _provider(tmp_path)
    with pytest.raises(ProviderError, match="chưa đăng nhập"):
        p.chat(ChatRequest(model=REF, system="S", messages=(Message.user_text("x"),)), cancel=CancelToken())


def test_map_429() -> None:
    assert isinstance(ClaudeCliProvider._map_failure(1, "x", api_status=429), RateLimited)
    assert isinstance(ClaudeCliProvider._map_failure(1, "rate limit exceeded"), RateLimited)


# ───── RunAgent (stream-json) ─────
def test_run_agent_streams_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stream")
    p = _provider(tmp_path)
    turns: list[TurnInfo] = []
    res = p.run_agent(
        session=_session(tmp_path),
        system="S",
        brief="B",
        model="claude-fake",
        effort=None,
        cancel=CancelToken(),
        on_turn=lambda t: turns.append(t) or True,
    )
    assert [t.index for t in turns] == [1, 2] and turns[0].tool_calls == ("mcp__ddt__fs_read",)
    assert (
        res.turns == 2
        and res.final_text == "done"
        and not res.is_error
        and not res.rate_limited
        and not res.killed
    )
    assert res.usage.tokens_in == 2400 and res.usage.tokens_out == 100 and res.exit_code == 0


def test_run_agent_killed_when_harness_says_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    p = _provider(tmp_path)
    seen: list[int] = []

    def on_turn(t: TurnInfo) -> bool:
        seen.append(t.index)
        return t.index < 3  # budget turn=3

    res = p.run_agent(
        session=_session(tmp_path),
        system="S",
        brief="B",
        model="claude-fake",
        effort=None,
        cancel=CancelToken(),
        on_turn=on_turn,
    )
    assert res.killed and 3 <= res.turns < 20 and res.subtype is None  # không tới dòng result


def test_run_agent_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limited")
    p = _provider(tmp_path)
    res = p.run_agent(
        session=_session(tmp_path),
        system="S",
        brief="B",
        model="claude-fake",
        effort=None,
        cancel=CancelToken(),
        on_turn=lambda t: True,
    )
    assert res.rate_limited and res.is_error and res.turns == 0


# ───── registry: alias song sinh · ưu tiên API cho role 1 call · cost 0 ─────
class _Fake:
    name = "anthropic_cli"
    caps = ClaudeCliProvider.caps

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:  # pragma: no cover
        raise NotImplementedError


def test_registry_cli_build_preset_without_api_key() -> None:
    cfg = load_config(preset="cli_build")
    reg = ModelRegistry(cfg.models, env={}, providers={"anthropic_cli": _Fake()})
    dev = reg.resolve("dev")
    assert dev.ref.alias == "opus_cli" and dev.fallback is None  # opus API thiếu key
    intake = reg.resolve("intake")  # haiku API thiếu key → song sinh haiku_cli
    assert intake.ref.alias == "haiku_cli" and any("haiku_cli" in n for n in intake.notes)


def test_registry_cli_build_with_api_key_prefers_api_for_single_call_roles() -> None:
    cfg = load_config(preset="cli_build")
    reg = ModelRegistry(cfg.models, env={"ANTHROPIC_API_KEY": "x"}, providers={"anthropic_cli": _Fake()})
    dev = reg.resolve("dev")
    assert (
        dev.ref.alias == "opus_cli" and dev.fallback is not None and dev.fallback.alias == "opus"
    )  # hết hạn mức → API
    raw = cfg.models.model_dump()
    raw["roles"]["intake"]["prefer"] = "haiku_cli"
    reg2 = ModelRegistry(
        type(cfg.models).model_validate(raw),
        env={"ANTHROPIC_API_KEY": "x"},
        providers={"anthropic_cli": _Fake()},
    )
    assert reg2.resolve("intake").ref.alias == "haiku"  # role 1 call: API trước agent_loop


def test_registry_cli_all_preset_cost_zero() -> None:
    cfg = load_config(preset="cli_all")
    reg = ModelRegistry(cfg.models, env={}, providers={"anthropic_cli": _Fake()})
    ref = reg.resolve("dev").ref
    assert ref.provider == "anthropic_cli"
    resp = ChatResponse(
        message=Message.user_text("x"),
        stop=StopReason.END_TURN,
        usage=Usage(tokens_in=1_000_000, tokens_out=1_000_000),
        model=ref.model,
    )
    assert reg.cost(ref, resp) == 0.0
    api_ref = ModelRef(provider="anthropic", model=ref.model, family="claude", alias="opus")
    assert reg.cost(api_ref, resp) > 0


def test_family_alias() -> None:
    from digital_development_team.llm.claude_cli_provider import family_alias, is_unrecognized_model

    assert family_alias("claude-sonnet-5-5") == "sonnet" and family_alias("claude-opus-5-5") == "opus"
    assert family_alias("claude-haiku-4-5-20251001") == "haiku" and family_alias("gpt-5") is None
    assert is_unrecognized_model('[claude-code:unrecognized_model] {"model":"x"}')


def test_unrecognized_model_falls_back_to_family_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "json")
    monkeypatch.setenv("FAKE_CLAUDE_ALIAS_ONLY", "1")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(argv_file))
    p = _provider(tmp_path)
    ref = ModelRef(provider="anthropic_cli", model="claude-sonnet-5-5", family="claude", alias="sonnet_cli")
    resp = p.chat(
        ChatRequest(model=ref, system="S", messages=(Message.user_text("ping"),)), cancel=CancelToken()
    )
    assert resp.text == "echo:ping" and p.cli_model("claude-sonnet-5-5") == "sonnet"
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--model") + 1] == "sonnet"
    # agent loop dùng luôn alias đã nhớ
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stream")
    res = p.run_agent(
        session=_session(tmp_path),
        system="S",
        brief="B",
        model="claude-sonnet-5-5",
        effort=None,
        cancel=CancelToken(),
        on_turn=lambda t: True,
    )
    assert res.turns == 2 and not res.is_error


def test_unrecognized_model_without_alias_is_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "json")
    monkeypatch.setenv("FAKE_CLAUDE_ALIAS_ONLY", "1")
    p = _provider(tmp_path)
    ref = ModelRef(provider="anthropic_cli", model="claude-fable-5-1", family="claude", alias="fable_cli")
    with pytest.raises(ProviderError) as ei:
        p.chat(ChatRequest(model=ref, system="S", messages=(Message.user_text("x"),)), cancel=CancelToken())
    assert (
        "không nhận model" in ei.value.message and not ei.value.retryable
    )  # không đẩy provider vào cooldown

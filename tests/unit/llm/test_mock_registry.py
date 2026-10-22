from __future__ import annotations

import json
from pathlib import Path

import pytest

from digital_development_team.config.loader import load_config
from digital_development_team.config.models import ModelsConfig
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.errors import (
    ConfigError,
    GoldenExhausted,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from digital_development_team.core.llm import (
    ChatRequest,
    ChatResponse,
    Message,
    ModelRef,
    ProviderCaps,
    StopReason,
    TextBlock,
    ToolSpec,
    Usage,
)
from digital_development_team.llm import MockProvider, ModelRegistry, RecordingProvider, ScriptStep

REF = ModelRef(provider="mock", model="mock", family="mock", alias="mock")


def _req(role: str = "dev", text: str = "hello", tools: bool = False) -> ChatRequest:
    return ChatRequest(
        model=REF,
        system="SYS allowed_files",
        messages=(Message.user_text(text),),
        tools=(
            ToolSpec(name="fs.read", wire_name="fs_read", description="d", input_schema={"type": "object"}),
        )
        if tools
        else (),
        metadata={"role": role, "issue": "DDT-0001"},
    )


def test_mock_echo_and_script(tmp_path: Path) -> None:
    m = MockProvider()
    r = m.chat(_req(), cancel=CancelToken())
    assert r.text.startswith("mock-echo: hello") and r.stop is StopReason.END_TURN
    m.add_script(
        "dev",
        [
            ScriptStep(text="đọc", tool_calls=[{"name": "fs_read", "input": {"path": "a.py"}}]),  # type: ignore[list-item]
            ScriptStep(
                text="xong", usage={"tokens_in": 10, "tokens_out": 2}, expect_in_prompt=["allowed_files"]
            ),  # type: ignore[arg-type]
        ],
    )
    r1 = m.chat(_req(tools=True), cancel=CancelToken())
    assert (
        r1.stop is StopReason.TOOL_USE
        and r1.tool_calls[0].name == "fs_read"
        and r1.tool_calls[0].id.startswith("toolu_mock_")
    )
    r2 = m.chat(_req(), cancel=CancelToken())
    assert r2.text == "xong" and r2.usage.tokens_in == 10
    with pytest.raises(GoldenExhausted):
        m.chat(_req(), cancel=CancelToken())
    # role khác không có script → echo
    assert m.chat(_req(role="leader"), cancel=CancelToken()).text.startswith("mock-echo")
    assert len(m.calls) == 5


def test_mock_expect_in_prompt_fails_early() -> None:
    m = MockProvider()
    m.add_script("*", [ScriptStep(text="x", expect_in_prompt=["KHÔNG CÓ"])])
    with pytest.raises(ProviderError, match="expect_in_prompt"):
        m.chat(_req(role="anything"), cancel=CancelToken())


def test_golden_file_roundtrip_via_recording(tmp_path: Path) -> None:
    inner = MockProvider()
    inner.add_script(
        "dev",
        [
            ScriptStep(text="ghi", tool_calls=[{"name": "fs_edit", "input": {"path": "a"}}]),
            ScriptStep(text="done"),
        ],
    )  # type: ignore[list-item]
    rec = RecordingProvider(inner, tmp_path / "golden")
    rec.chat(_req(), cancel=CancelToken())
    rec.chat(_req(), cancel=CancelToken())
    rec.chat(_req(role="leader"), cancel=CancelToken())
    files = rec.save(case="calc")
    assert {p.name for p in files} == {"dev.json", "leader.json"}
    data = json.loads((tmp_path / "golden" / "dev.json").read_text(encoding="utf-8"))
    assert (
        data["role"] == "dev"
        and len(data["steps"]) == 2
        and data["steps"][0]["tool_calls"][0]["name"] == "fs_edit"
    )
    replay = MockProvider(echo=False)
    assert replay.load_dir(tmp_path / "golden") == 2
    assert replay.chat(_req(), cancel=CancelToken()).tool_calls[0].name == "fs_edit"
    assert replay.chat(_req(role="leader"), cancel=CancelToken()).text.startswith(
        "mock-echo"
    )  # recorded echo
    with pytest.raises(ProviderError):
        replay.chat(_req(role="nobody"), cancel=CancelToken())  # echo tắt, không script


# ───── registry ─────
@pytest.fixture
def models() -> ModelsConfig:
    return load_config().models


def test_resolve_default_requires_key(models: ModelsConfig) -> None:
    reg = ModelRegistry(models, env={})
    with pytest.raises(ConfigError, match="thiếu \\$ANTHROPIC_API_KEY"):
        reg.resolve("dev")
    reg2 = ModelRegistry(models, env={"ANTHROPIC_API_KEY": "sk"})
    r = reg2.resolve("dev")
    assert r.ref.alias == "opus" and r.fallback is not None and r.fallback.alias == "sonnet"
    v = reg2.resolve("verifier")
    assert v.ref.alias == "sonnet" and any("cùng họ" in n for n in v.notes)  # không có alt_mid


def test_resolve_prefers_other_family(models: ModelsConfig) -> None:
    cfg = load_config(preset="mixed_verifier").models
    reg = ModelRegistry(cfg, env={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"})
    v = reg.resolve("verifier")
    assert (
        v.ref.alias == "alt_mid"
        and v.ref.family == "gpt"
        and v.fallback is not None
        and v.fallback.alias == "sonnet"
    )
    # thiếu key openai → alt_mid không dùng được → về sonnet, ghi chú
    reg2 = ModelRegistry(cfg, env={"ANTHROPIC_API_KEY": "a"})
    v2 = reg2.resolve("verifier")
    assert v2.ref.alias == "sonnet" and any("alt_mid: thiếu $OPENAI_API_KEY" in n for n in v2.notes)


def test_resolve_override_mock_and_unknown_role(models: ModelsConfig) -> None:
    reg = ModelRegistry(models, env={}, provider_override="mock")
    assert reg.resolve("dev").ref.provider == "mock"
    with pytest.raises(ConfigError, match="role 'nope'"):
        reg.resolve("nope")


def test_resolve_disabled_provider_falls_back(models: ModelsConfig) -> None:
    cfg = load_config(preset="all_openai").models  # anthropic tắt
    reg = ModelRegistry(cfg, env={"OPENAI_API_KEY": "o"})
    assert reg.resolve("intake").ref.provider == "openai"
    assert reg.unavailable_reason("gemini") == "provider google tắt"


class _Flaky:
    name = "mock"
    caps = ProviderCaps()

    def __init__(self, fail: int, exc: type[Exception] = RateLimited) -> None:
        self.fail, self.exc, self.n = fail, exc, 0

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        self.n += 1
        if self.n <= self.fail:
            raise self.exc("boom")
        return ChatResponse(
            message=Message(role="assistant", blocks=(TextBlock(text=f"ok from {req.model.alias}"),)),
            stop=StopReason.END_TURN,
            usage=Usage(tokens_in=100, tokens_out=10),
            model=req.model.model,
        )


def test_client_retries_then_ok(models: ModelsConfig, fake_clock: FakeClock) -> None:
    cfg = models.model_copy(
        update={"rate": models.rate.model_copy(update={"backoff": [0.0], "on_429_consecutive": 5})}
    )
    flaky = _Flaky(fail=2)
    reg = ModelRegistry(cfg, env={}, provider_override="mock", providers={"mock": flaky}, clock=fake_clock)
    out = reg.client("dev").chat(_req(), cancel=CancelToken())
    assert out.response.text == "ok from mock" and out.attempts == 3 and not out.fell_back
    assert out.retries == ("RATE_LIMITED → chờ 0s", "RATE_LIMITED → chờ 0s")
    assert out.cost_usd == 0.0


def test_client_falls_back_to_other_alias(fake_clock: FakeClock) -> None:
    cfg = load_config(preset="mixed_verifier").models
    cfg = cfg.model_copy(
        update={"rate": cfg.rate.model_copy(update={"backoff": [0.0], "on_429_consecutive": 2})}
    )
    down = _Flaky(fail=99, exc=ProviderUnavailable)
    ok = _Flaky(fail=0)
    ok.name = "anthropic"
    reg = ModelRegistry(
        cfg,
        env={"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a"},
        providers={"openai": down, "anthropic": ok},
        clock=fake_clock,
    )
    client = reg.client("verifier")
    assert client.ref.alias == "alt_mid"
    out = client.chat(_req(role="verifier"), cancel=CancelToken())
    assert out.fell_back and out.ref.alias == "sonnet" and out.response.text == "ok from sonnet"
    assert any("fallback sonnet" in r for r in out.retries)
    assert reg.in_cooldown("openai")
    # lần resolve sau bỏ qua provider đang cooldown
    assert reg.resolve("verifier").ref.alias == "sonnet"
    assert out.cost_usd == pytest.approx((100 * 3.0 + 10 * 15.0) / 1_000_000)


def test_client_tool_use_unsupported(models: ModelsConfig) -> None:
    class NoTools(_Flaky):
        caps = ProviderCaps(tool_use=False)

    reg = ModelRegistry(models, env={}, provider_override="mock", providers={"mock": NoTools(0)})
    with pytest.raises(ProviderError, match="tool_use"):
        reg.client("dev").chat(_req(tools=True), cancel=CancelToken())

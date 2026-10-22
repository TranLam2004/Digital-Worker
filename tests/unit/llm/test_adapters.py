"""Adapter test bằng client giả — không mạng, không key. Kiểm chuyển đổi request/response + map lỗi."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import anthropic
import httpx
import openai
import pytest

from digital_development_team.config.models import ProviderCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import (
    Cancelled,
    ContextOverflow,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from digital_development_team.core.llm import (
    ChatRequest,
    Message,
    ModelRef,
    OpaqueBlock,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from digital_development_team.llm import anthropic_provider as ap
from digital_development_team.llm import openai_provider as op

REF_A = ModelRef(
    provider="anthropic", model="claude-sonnet-5-5", family="claude", alias="sonnet", effort="medium"
)
REF_O = ModelRef(provider="openai", model="gpt-5", family="gpt", alias="gpt", effort="high")
TOOL = ToolSpec(
    name="fs.read",
    wire_name="fs_read",
    description="đọc file",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
)


def _req(ref: ModelRef) -> ChatRequest:
    return ChatRequest(
        model=ref,
        system="SYS",
        messages=(
            Message.user_text("hi"),
            Message(
                role="assistant",
                blocks=(
                    OpaqueBlock(
                        provider="anthropic", data={"type": "thinking", "thinking": "…", "signature": "sig"}
                    ),
                    TextBlock(text="đọc đã"),
                    ToolUseBlock(id="t1", name="fs_read", input={"path": "a.py"}),
                ),
            ),
            Message(
                role="user",
                blocks=(ToolResultBlock(tool_use_id="t1", content="nội dung"), TextBlock(text="tiếp")),
            ),
        ),
        tools=(TOOL,),
        temperature=0.0,
        metadata={"role": "dev", "issue": "DDT-0001"},
    )


# ───── Anthropic ─────
def test_anthropic_build_params() -> None:
    p = ap.build_params(
        _req(REF_A),
        caps=ap.AnthropicProvider.__dict__.get("caps") or ap.ProviderCaps(prompt_cache=True),
        supports_effort=True,
    )
    assert p["model"] == "claude-sonnet-5-5" and p["max_tokens"] == 8192
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["tools"][0]["name"] == "fs_read" and p["tool_choice"] == {"type": "auto"}
    assert p["output_config"] == {"effort": "medium"} and p["metadata"]["user_id"] == "DDT-0001/dev"
    asst = p["messages"][1]["content"]
    assert asst[0] == {"type": "thinking", "thinking": "…", "signature": "sig"}  # opaque gửi lại nguyên vẹn
    assert asst[2]["type"] == "tool_use" and asst[2]["input"] == {"path": "a.py"}
    assert p["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "nội dung",
        "is_error": False,
    }
    p2 = ap.build_params(_req(REF_A), caps=ap.ProviderCaps(prompt_cache=False), supports_effort=False)
    assert p2["system"] == "SYS" and "output_config" not in p2


def test_anthropic_from_message_and_usage() -> None:
    msg = NS(
        content=[
            NS(
                type="thinking",
                thinking="x",
                signature="s",
                model_dump=lambda: {"type": "thinking", "thinking": "x", "signature": "s"},
            ),
            NS(type="text", text="ok"),
            NS(type="tool_use", id="tu1", name="fs_read", input={"path": "a"}),
        ],
        usage=NS(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=400, cache_creation_input_tokens=50
        ),
        stop_reason="tool_use",
        model="claude-sonnet-5-5",
        id="msg_1",
    )
    r = ap.from_message(msg, latency_ms=12)
    assert r.stop is StopReason.TOOL_USE and r.text == "ok" and r.tool_calls[0].id == "tu1"
    assert isinstance(r.message.blocks[0], OpaqueBlock)
    assert r.usage.tokens_in == 550 and r.usage.tokens_cached == 400 and r.usage.tokens_out == 20


class _FakeStream:
    def __init__(self, msg: Any, events: int = 3, cancel: CancelToken | None = None) -> None:
        self.msg, self.events, self.cancel = msg, events, cancel

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def __iter__(self) -> Any:
        for i in range(self.events):
            if self.cancel is not None and i == 1:
                self.cancel.cancel("user")
            yield i

    def get_final_message(self) -> Any:
        return self.msg


def _anthropic_cfg() -> ProviderCfg:
    return ProviderCfg(
        adapter="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        supports=["tool_use", "prompt_cache", "streaming"],
        enabled=True,
    )


def test_anthropic_provider_chat_and_cancel() -> None:
    final = NS(
        content=[NS(type="text", text="pong")],
        usage=NS(input_tokens=5, output_tokens=1),
        stop_reason="end_turn",
        model="m",
        id="i",
    )
    calls: list[dict[str, Any]] = []
    client = NS(messages=NS(stream=lambda **kw: (calls.append(kw), _FakeStream(final))[1]))
    prov = ap.AnthropicProvider(_anthropic_cfg(), api_key="k", client=client)
    r = prov.chat(_req(REF_A), cancel=CancelToken())
    assert r.text == "pong" and calls[0]["model"] == "claude-sonnet-5-5"
    cancel = CancelToken()
    client2 = NS(messages=NS(stream=lambda **kw: _FakeStream(final, cancel=cancel)))
    with pytest.raises(Cancelled):
        ap.AnthropicProvider(_anthropic_cfg(), api_key="k", client=client2).chat(_req(REF_A), cancel=cancel)


def _resp(status: int, body: str = "{}") -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://x"), text=body)


def test_anthropic_map_error() -> None:
    assert isinstance(
        ap.map_error(anthropic.RateLimitError("r", response=_resp(429), body=None)), RateLimited
    )
    assert isinstance(
        ap.map_error(anthropic.InternalServerError("s", response=_resp(529), body=None)), ProviderUnavailable
    )
    assert isinstance(
        ap.map_error(anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))),
        ProviderUnavailable,
    )
    assert isinstance(
        ap.map_error(
            anthropic.BadRequestError("prompt is too long: 210000 tokens", response=_resp(400), body=None)
        ),
        ContextOverflow,
    )
    e = ap.map_error(anthropic.BadRequestError("invalid tool schema", response=_resp(400), body=None))
    assert type(e) is ProviderError and not e.retryable
    assert "ANTHROPIC_API_KEY" in (
        ap.map_error(anthropic.AuthenticationError("a", response=_resp(401), body=None)).hint or ""
    )


# ───── OpenAI-compatible ─────
def test_openai_build_params_and_messages() -> None:
    p = op.build_params(_req(REF_O), adapter="openai")
    assert p["max_completion_tokens"] == 8192 and p["reasoning_effort"] == "high" and p["temperature"] == 0.0
    msgs = p["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[2]["role"] == "assistant" and msgs[2]["content"] == "đọc đã"
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "fs_read", "arguments": '{"path": "a.py"}'}
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "nội dung"}
    assert msgs[4] == {"role": "user", "content": "tiếp"}
    assert p["tools"][0]["function"]["name"] == "fs_read" and p["tool_choice"] == "auto"
    p2 = op.build_params(_req(REF_O), adapter="openai_compatible")
    assert p2["max_tokens"] == 8192 and "reasoning_effort" not in p2


def test_openai_from_completion() -> None:
    resp = NS(
        choices=[
            NS(
                finish_reason="tool_calls",
                message=NS(
                    content=None,
                    tool_calls=[NS(id="c1", function=NS(name="fs_read", arguments='{"path": "a"}'))],
                ),
            )
        ],
        usage=NS(prompt_tokens=120, completion_tokens=8, prompt_tokens_details=NS(cached_tokens=100)),
        model="gpt-5",
        id="cmpl",
    )
    r = op.from_completion(resp, latency_ms=3)
    assert (
        r.stop is StopReason.TOOL_USE
        and r.tool_calls[0].input == {"path": "a"}
        and r.usage.tokens_cached == 100
    )
    bad = NS(
        choices=[
            NS(
                finish_reason="stop",
                message=NS(content="x", tool_calls=[NS(id="c", function=NS(name="f", arguments="{oops"))]),
            )
        ],
        usage=None,
        model="m",
        id=None,
    )
    r2 = op.from_completion(bad, latency_ms=0)
    assert r2.tool_calls[0].input == {"_raw": "{oops"} and r2.usage.tokens_in == 0


def test_openai_provider_chat_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ProviderCfg(
        adapter="openai_compatible", base_url="${MY_BASE}/v1", supports=["tool_use"], enabled=True
    )
    monkeypatch.setenv("MY_BASE", "http://localhost:11434")
    assert op.expand_env(cfg.base_url) == "http://localhost:11434/v1"
    good = NS(
        choices=[NS(finish_reason="stop", message=NS(content="pong", tool_calls=None))],
        usage=NS(prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None),
        model="q",
        id="x",
    )
    client = NS(chat=NS(completions=NS(create=lambda **kw: good)))
    prov = op.OpenAICompatProvider("local", cfg, api_key=None, client=client)
    assert prov.chat(_req(REF_O), cancel=CancelToken()).text == "pong"

    def boom(**kw: Any) -> Any:
        raise openai.RateLimitError("429", response=_resp(429), body=None)

    prov2 = op.OpenAICompatProvider(
        "local", cfg, api_key=None, client=NS(chat=NS(completions=NS(create=boom)))
    )
    with pytest.raises(RateLimited):
        prov2.chat(_req(REF_O), cancel=CancelToken())
    assert isinstance(
        op.map_error(
            openai.BadRequestError("context_length_exceeded", response=_resp(400), body=None), "openai"
        ),
        ContextOverflow,
    )
    assert isinstance(
        op.map_error(openai.InternalServerError("x", response=_resp(500), body=None), "openai"),
        ProviderUnavailable,
    )

"""Adapter Chat Completions: OpenAI · Azure OpenAI · OpenAI-compatible (Ollama, vLLM, LM Studio).

- Tool calling → ToolUseBlock; ToolResultBlock → message role=tool.
- OpaqueBlock (của provider khác) bị bỏ qua — Chat Completions không có kênh gửi lại.
- adapter=openai dùng max_completion_tokens + reasoning_effort; openai_compatible dùng max_tokens.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from digital_development_team.config.models import ProviderCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import (
    ContextOverflow,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from digital_development_team.core.llm import (
    Block,
    ChatRequest,
    ChatResponse,
    Message,
    ProviderCaps,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

_FINISH = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}
_OVERFLOW_HINTS = ("context_length_exceeded", "maximum context length", "too many tokens", "context window")
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def expand_env(value: str | None, env: dict[str, str] | os._Environ[str] | None = None) -> str | None:
    if value is None:
        return None
    e = env if env is not None else os.environ
    return _ENV_RE.sub(lambda m: e.get(m.group(1), ""), value)


def _messages(req: ChatRequest) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if req.system:
        out.append({"role": "system", "content": req.system})
    for m in req.messages:
        if m.role == "user":
            texts = [b.text for b in m.blocks if isinstance(b, TextBlock)]
            results = [b for b in m.blocks if isinstance(b, ToolResultBlock)]
            for r in results:  # tool result phải đứng ngay sau assistant tool_calls
                out.append({"role": "tool", "tool_call_id": r.tool_use_id, "content": r.content})
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
        else:
            text = "".join(b.text for b in m.blocks if isinstance(b, TextBlock))
            calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)},
                }
                for b in m.blocks
                if isinstance(b, ToolUseBlock)
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def build_params(req: ChatRequest, *, adapter: str) -> dict[str, Any]:
    params: dict[str, Any] = {"model": req.model.model, "messages": _messages(req)}
    if adapter == "openai":
        params["max_completion_tokens"] = req.max_tokens
        if req.model.effort:
            params["reasoning_effort"] = req.model.effort
    else:
        params["max_tokens"] = req.max_tokens
    if req.tools:
        params["tools"] = [
            {
                "type": "function",
                "function": {"name": t.wire_name, "description": t.description, "parameters": t.input_schema},
            }
            for t in req.tools
        ]
        params["tool_choice"] = "auto"
    if req.temperature is not None:
        params["temperature"] = req.temperature
    return params


def from_completion(resp: Any, *, latency_ms: int) -> ChatResponse:
    choice = resp.choices[0]
    msg = choice.message
    blocks: list[Block] = []
    if msg.content:
        blocks.append(TextBlock(text=str(msg.content)))
    for tc in msg.tool_calls or ():
        args = tc.function.arguments or "{}"
        try:
            inp = json.loads(args)
        except json.JSONDecodeError:
            inp = {"_raw": args}
        blocks.append(
            ToolUseBlock(id=tc.id, name=tc.function.name, input=inp if isinstance(inp, dict) else {"_": inp})
        )
    if not blocks:
        blocks.append(TextBlock(text=""))
    u = resp.usage
    cached = 0
    details = getattr(u, "prompt_tokens_details", None) if u else None
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    usage = Usage(
        tokens_in=int(getattr(u, "prompt_tokens", 0) or 0) if u else 0,
        tokens_out=int(getattr(u, "completion_tokens", 0) or 0) if u else 0,
        tokens_cached=cached,
        latency_ms=latency_ms,
    )
    return ChatResponse(
        message=Message(role="assistant", blocks=tuple(blocks)),
        stop=_FINISH.get(str(choice.finish_reason), StopReason.OTHER),
        usage=usage,
        model=str(resp.model),
        raw_id=getattr(resp, "id", None),
    )


def map_error(e: Exception, provider: str) -> ProviderError:
    import openai

    msg = str(e)
    if isinstance(e, openai.RateLimitError):
        return RateLimited(f"{provider} 429: {msg[:200]}")
    if isinstance(e, openai.BadRequestError):
        low = msg.lower()
        if any(h in low for h in _OVERFLOW_HINTS):
            return ContextOverflow(f"{provider}: {msg[:200]}")
        return ProviderError(f"{provider} 400: {msg[:300]}")
    if isinstance(e, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderError(f"{provider} auth: {msg[:200]}", hint="kiểm api_key_env trong models.yaml")
    if isinstance(e, openai.APIConnectionError | openai.InternalServerError):
        return ProviderUnavailable(f"{provider}: {msg[:200]}")
    if isinstance(e, openai.APIStatusError):
        if e.status_code >= 500:
            return ProviderUnavailable(f"{provider} {e.status_code}: {msg[:200]}")
        return ProviderError(f"{provider} {e.status_code}: {msg[:300]}")
    return ProviderError(f"{provider}: {msg[:300]}")


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        cfg: ProviderCfg,
        *,
        api_key: str | None,
        client: Any = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.name = name
        self._adapter = cfg.adapter
        self.caps = ProviderCaps(
            tool_use="tool_use" in cfg.supports,
            prompt_cache="prompt_cache" in cfg.supports,
            streaming="streaming" in cfg.supports,
            append_only_history=False,
        )
        if client is None:
            import openai

            client = openai.OpenAI(
                api_key=api_key or "not-needed",
                base_url=expand_env(cfg.base_url),
                max_retries=0,
                timeout=timeout_s,
            )
        self._client = client

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        cancel.raise_if_cancelled()
        params = build_params(req, adapter=self._adapter)
        t0 = time.monotonic()
        try:
            resp = self._client.chat.completions.create(**params)
        except ProviderError:
            raise
        except Exception as e:
            raise map_error(e, self.name) from e
        return from_completion(resp, latency_ms=int((time.monotonic() - t0) * 1000))

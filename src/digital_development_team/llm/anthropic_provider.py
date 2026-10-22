"""Adapter Anthropic Messages API. SDK max_retries=0 (retry tập trung ở llm.retry).

- Tool cố định cả phiên; history append-only; OpaqueBlock (thinking + signature) gửi lại nguyên vẹn.
- stream + get_final_message: kiểm cancel giữa các event, tránh timeout request dài.
- cache_control trên system block → cache cả tools + system (thứ tự cache: tools → system → messages).
"""

from __future__ import annotations

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
    OpaqueBlock,
    ProviderCaps,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

PROVIDER = "anthropic"
_STOP = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.PAUSE,
}
_OVERFLOW_HINTS = ("prompt is too long", "too many tokens", "exceeds the maximum", "context window")


# ───── chuyển đổi thuần (test được không cần SDK) ─────
def _block_param(b: Block) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": b.tool_use_id,
            "content": b.content,
            "is_error": b.is_error,
        }
    if isinstance(b, OpaqueBlock):
        return dict(b.data)
    raise ProviderError(f"block lạ: {type(b).__name__}")  # pragma: no cover


def build_params(req: ChatRequest, *, caps: ProviderCaps, supports_effort: bool) -> dict[str, Any]:
    system: Any = req.system
    if req.cache_system and caps.prompt_cache and req.system:
        system = [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
    params: dict[str, Any] = {
        "model": req.model.model,
        "max_tokens": req.max_tokens,
        "messages": [{"role": m.role, "content": [_block_param(b) for b in m.blocks]} for m in req.messages],
    }
    if req.system:
        params["system"] = system
    if req.tools:
        params["tools"] = [
            {"name": t.wire_name, "description": t.description, "input_schema": t.input_schema}
            for t in req.tools
        ]
        params["tool_choice"] = {"type": "auto"}
    if req.temperature is not None:
        params["temperature"] = req.temperature
    if req.model.effort and supports_effort:
        params["output_config"] = {"effort": req.model.effort}
    user_id = "/".join(v for k, v in sorted(req.metadata.items()) if k in ("issue", "role"))
    if user_id:
        params["metadata"] = {"user_id": user_id[:256]}
    return params


def from_message(msg: Any, *, latency_ms: int) -> ChatResponse:
    """msg: anthropic.types.Message (duck-typed: .content .usage .stop_reason .model .id)."""
    blocks: list[Block] = []
    for b in msg.content:
        t = getattr(b, "type", None)
        if t == "text":
            blocks.append(TextBlock(text=b.text))
        elif t == "tool_use":
            blocks.append(ToolUseBlock(id=b.id, name=b.name, input=dict(b.input or {})))
        else:  # thinking · redacted_thinking · server_tool_use … giữ nguyên để gửi lại
            data = b.model_dump() if hasattr(b, "model_dump") else dict(b)
            blocks.append(OpaqueBlock(provider=PROVIDER, data=data))
    u = msg.usage
    cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    usage = Usage(
        tokens_in=int(u.input_tokens or 0) + cache_read + cache_write,
        tokens_out=int(u.output_tokens or 0),
        tokens_cached=cache_read,
        latency_ms=latency_ms,
    )
    return ChatResponse(
        message=Message(role="assistant", blocks=tuple(blocks)),
        stop=_STOP.get(str(msg.stop_reason), StopReason.OTHER),
        usage=usage,
        model=str(msg.model),
        raw_id=getattr(msg, "id", None),
    )


def map_error(e: Exception) -> ProviderError:
    """Ánh xạ lỗi SDK → DDTError. Import lười để module không phụ thuộc SDK lúc import."""
    import anthropic

    msg = str(e)
    if isinstance(e, anthropic.RateLimitError):
        return RateLimited(f"anthropic 429: {msg[:200]}")
    if isinstance(e, anthropic.BadRequestError):
        low = msg.lower()
        if any(h in low for h in _OVERFLOW_HINTS):
            return ContextOverflow(f"anthropic: {msg[:200]}")
        return ProviderError(f"anthropic 400: {msg[:300]}")
    if isinstance(e, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderError(f"anthropic auth: {msg[:200]}", hint="kiểm ANTHROPIC_API_KEY")
    if isinstance(e, anthropic.APIConnectionError | anthropic.InternalServerError):
        return ProviderUnavailable(f"anthropic: {msg[:200]}")
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code in (429,):
            return RateLimited(f"anthropic {e.status_code}: {msg[:200]}")
        if e.status_code >= 500 or e.status_code == 529:
            return ProviderUnavailable(f"anthropic {e.status_code}: {msg[:200]}")
        return ProviderError(f"anthropic {e.status_code}: {msg[:300]}")
    return ProviderError(f"anthropic: {msg[:300]}")


class AnthropicProvider:
    name = PROVIDER

    def __init__(
        self,
        cfg: ProviderCfg,
        *,
        api_key: str | None,
        client: Any = None,
        timeout_s: float = 600.0,
    ) -> None:
        self._cfg = cfg
        self.caps = ProviderCaps(
            tool_use="tool_use" in cfg.supports,
            prompt_cache="prompt_cache" in cfg.supports,
            streaming=True,
            append_only_history=True,
        )
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                api_key=api_key, base_url=cfg.base_url, max_retries=0, timeout=timeout_s
            )
            self._supports_effort = "effort" in getattr(
                anthropic.types.OutputConfigParam, "__annotations__", {}
            )
        else:
            self._supports_effort = True
        self._client = client

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        cancel.raise_if_cancelled()
        params = build_params(req, caps=self.caps, supports_effort=self._supports_effort)
        t0 = time.monotonic()
        try:
            with self._client.messages.stream(**params) as stream:
                for _ in stream:
                    cancel.raise_if_cancelled()
                final = stream.get_final_message()
        except ProviderError:
            raise
        except Exception as e:
            from digital_development_team.core.errors import Cancelled

            if isinstance(e, Cancelled):
                raise
            raise map_error(e) from e
        return from_message(final, latency_ms=int((time.monotonic() - t0) * 1000))

"""Kiểu dữ liệu trung lập với provider. OpaqueBlock giữ nguyên block đặc thù (thinking) để gửi lại."""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ───── Blocks ─────
class TextBlock(_Frozen):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(_Frozen):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str  # wire name: fs_read
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(_Frozen):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class OpaqueBlock(_Frozen):
    """Block provider-specific (thinking + signature…). Harness KHÔNG đọc, chỉ gửi lại nguyên vẹn."""

    type: Literal["opaque"] = "opaque"
    provider: str
    data: dict[str, Any]


Block = Annotated[TextBlock | ToolUseBlock | ToolResultBlock | OpaqueBlock, Field(discriminator="type")]


class Message(_Frozen):
    role: Literal["user", "assistant"]
    blocks: tuple[Block, ...]

    @staticmethod
    def user_text(text: str) -> Message:
        return Message(role="user", blocks=(TextBlock(text=text),))

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))


# ───── Tool spec gửi lên provider ─────
class ToolSpec(_Frozen):
    name: str  # canonical: fs.read
    wire_name: str  # fs_read — API chỉ nhận ^[a-zA-Z0-9_-]{1,64}$
    description: str
    input_schema: dict[str, Any]
    strict: bool = True


# ───── Request / Response ─────
class ModelRef(_Frozen):
    provider: str
    model: str
    family: str
    alias: str
    effort: Literal["low", "medium", "high"] | None = None


class ProviderCaps(_Frozen):
    tool_use: bool = True
    prompt_cache: bool = False
    streaming: bool = True
    append_only_history: bool = False  # True → không được sửa message cũ (preserved thinking)
    max_context_tokens: int = 200_000
    agent_loop: bool = (
        False  # True → adapter tự chạy loop (RunAgent, vd Claude Code CLI); harness chỉ nhận usage/outcome
    )


class Usage(_Frozen):
    """Đã chuẩn hóa: tokens_in = uncached + cache_read + cache_write."""

    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    latency_ms: int = 0

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out


@unique
class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    PAUSE = "pause"
    OTHER = "other"


class ChatRequest(_Frozen):
    model: ModelRef
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int = 8192
    temperature: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    cache_system: bool = True


class ChatResponse(_Frozen):
    message: Message
    stop: StopReason
    usage: Usage
    model: str
    raw_id: str | None = None

    @property
    def tool_calls(self) -> tuple[ToolUseBlock, ...]:
        return self.message.tool_uses

    @property
    def text(self) -> str:
        return self.message.text

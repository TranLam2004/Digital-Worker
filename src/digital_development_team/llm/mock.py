"""MockProvider — chạy offline, 0 token, deterministic. RecordingProvider — ghi golden từ provider thật.

Golden file (JSON) `testdata/golden/<role>.json`:
{
  "role": "dev",                         # hoặc "*" = mặc định cho mọi role
  "steps": [
    {"text": "Đọc file trước", "tool_calls": [{"name": "fs_read", "input": {"path": "calc.py"}}], "stop": "tool_use"},
    {"text": "Xong", "stop": "end_turn", "usage": {"tokens_in": 1200, "tokens_out": 80},
     "expect_in_prompt": ["allowed_files"]}   # kiểm brief có chứa → sai brief fail sớm
  ]
}
Hết steps → GoldenExhausted. Không có script và echo=True → trả lời echo (dry-run/llm-ping).
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import GoldenExhausted, ProviderError
from digital_development_team.core.llm import (
    ChatRequest,
    ChatResponse,
    Message,
    ProviderCaps,
    StopReason,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from digital_development_team.core.protocols import LLMProvider
from digital_development_team.util import jsonio
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.text import estimate_tokens

DEFAULT_ROLE = "*"


class ScriptToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str  # wire name
    input: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class ScriptUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0


class ScriptStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str = ""
    tool_calls: list[ScriptToolCall] = Field(default_factory=list)
    stop: Literal["end_turn", "tool_use", "max_tokens", "refusal", "pause", "other"] | None = None
    usage: ScriptUsage | None = None
    expect_in_prompt: list[str] = Field(default_factory=list)
    note: str | None = None

    def stop_reason(self) -> StopReason:
        if self.stop:
            return StopReason(self.stop)
        return StopReason.TOOL_USE if self.tool_calls else StopReason.END_TURN


class GoldenFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: str = DEFAULT_ROLE
    case: str | None = None
    steps: list[ScriptStep]


def _prompt_text(req: ChatRequest) -> str:
    parts = [req.system]
    for m in req.messages:
        parts.append(m.text)
    return "\n".join(parts)


class MockProvider:
    name = "mock"
    caps = ProviderCaps(tool_use=True, prompt_cache=True, streaming=True, append_only_history=False)

    def __init__(
        self,
        scripts: Mapping[str, Sequence[ScriptStep]] | None = None,
        *,
        echo: bool = True,
        latency_ms: int = 1,
    ) -> None:
        self._queues: dict[str, deque[ScriptStep]] = {k: deque(v) for k, v in (scripts or {}).items()}
        self._echo = echo
        self._latency_ms = latency_ms
        self._lock = threading.Lock()
        self._n = 0
        self.calls: list[ChatRequest] = []

    # ───── nạp golden ─────
    def add_script(self, role: str, steps: Sequence[ScriptStep]) -> None:
        with self._lock:
            self._queues.setdefault(role, deque()).extend(steps)

    def load(self, path: Path) -> GoldenFile:
        g = GoldenFile.model_validate_json(path.read_text(encoding=ENCODING))
        self.add_script(g.role, g.steps)
        return g

    def load_dir(self, directory: Path) -> int:
        n = 0
        for p in sorted(directory.glob("*.json")):
            self.load(p)
            n += 1
        return n

    def remaining(self, role: str) -> int:
        return len(self._queues.get(role, ()))

    # ───── chat ─────
    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        cancel.raise_if_cancelled()
        role = req.metadata.get("role", DEFAULT_ROLE)
        with self._lock:
            self.calls.append(req)
            self._n += 1
            n = self._n
            q = self._queues.get(role)
            if q is None or (not q and self._queues.get(DEFAULT_ROLE)):
                q = self._queues.get(DEFAULT_ROLE)
            step = q.popleft() if q else None
            if step is None and q is not None:  # có script nhưng hết lượt
                raise GoldenExhausted(f"golden cho role {role!r} hết sau {n} lượt", role=role)
        if step is None:
            if not self._echo:
                raise ProviderError(f"mock: không có script cho role {role!r} và echo tắt", role=role)
            return self._echo_response(req)
        prompt = _prompt_text(req)
        missing = [s for s in step.expect_in_prompt if s not in prompt]
        if missing:
            raise ProviderError(f"golden expect_in_prompt thiếu trong brief: {missing}", role=role, step=n)
        blocks: list[TextBlock | ToolUseBlock] = []
        if step.text:
            blocks.append(TextBlock(text=step.text))
        for i, tc in enumerate(step.tool_calls):
            blocks.append(ToolUseBlock(id=tc.id or f"toolu_mock_{n}_{i}", name=tc.name, input=dict(tc.input)))
        if not blocks:
            blocks.append(TextBlock(text=""))
        usage = (
            Usage(
                tokens_in=step.usage.tokens_in,
                tokens_out=step.usage.tokens_out,
                tokens_cached=step.usage.tokens_cached,
                latency_ms=self._latency_ms,
            )
            if step.usage
            else Usage(
                tokens_in=estimate_tokens(prompt),
                tokens_out=estimate_tokens(step.text),
                latency_ms=self._latency_ms,
            )
        )
        return ChatResponse(
            message=Message(role="assistant", blocks=tuple(blocks)),
            stop=step.stop_reason(),
            usage=usage,
            model="mock",
            raw_id=f"mock_{n}",
        )

    def _echo_response(self, req: ChatRequest) -> ChatResponse:
        last_user = next((m.text for m in reversed(req.messages) if m.role == "user"), "")
        text = f"mock-echo: {last_user[:200]}"
        return ChatResponse(
            message=Message(role="assistant", blocks=(TextBlock(text=text),)),
            stop=StopReason.END_TURN,
            usage=Usage(
                tokens_in=estimate_tokens(_prompt_text(req)),
                tokens_out=estimate_tokens(text),
                latency_ms=self._latency_ms,
            ),
            model="mock",
        )


class RecordingProvider:
    """Bọc provider thật, ghi lại từng response thành ScriptStep theo role. save() → golden dir."""

    def __init__(self, inner: LLMProvider, out_dir: Path) -> None:
        self._inner = inner
        self._out = out_dir
        self._lock = threading.Lock()
        self._steps: dict[str, list[ScriptStep]] = {}

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def caps(self) -> ProviderCaps:
        return self._inner.caps

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        resp = self._inner.chat(req, cancel=cancel)
        step = ScriptStep(
            text=resp.text,
            tool_calls=[ScriptToolCall(name=t.name, input=dict(t.input), id=t.id) for t in resp.tool_calls],
            stop=resp.stop.value,
            usage=ScriptUsage(
                tokens_in=resp.usage.tokens_in,
                tokens_out=resp.usage.tokens_out,
                tokens_cached=resp.usage.tokens_cached,
            ),
            note=f"recorded from {resp.model}",
        )
        role = req.metadata.get("role", DEFAULT_ROLE)
        with self._lock:
            self._steps.setdefault(role, []).append(step)
        return resp

    def save(self, *, case: str | None = None) -> list[Path]:
        self._out.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        with self._lock:
            for role, steps in self._steps.items():
                g = GoldenFile(role=role, case=case, steps=steps)
                p = self._out / f"{role}.json"
                write_text_atomic(p, jsonio.dumps(json.loads(g.model_dump_json(exclude_none=True))) + "\n")
                written.append(p)
        return written

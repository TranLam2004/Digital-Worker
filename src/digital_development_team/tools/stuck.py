"""Phát hiện agent bị kẹt (OpenHands stuck detector), gắn trong Dispatcher nên phủ cả API loop lẫn CLI qua MCP.

Mỗi (issue, actor) trong một lần chạy agent:
  - cùng tool + cùng args + cùng kết quả `repeat` lần liên tiếp → gợi ý đổi cách làm
  - `denials` lần liên tiếp bị policy từ chối → gợi ý escalate kèm spec_patch thay vì thử biến thể
Lần kẹt thứ `stop_after` → kết quả mang stuck.stop: API loop kết thúc lượt bằng `STUCK` (recovery lớp `stuck`),
mọi tool call sau đó trong lần chạy (CLI) trả lỗi STUCK để agent dừng.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field

from digital_development_team.core.models import ToolResult

REPEAT_HINT = (
    "You called {tool} {n} times with the same arguments and got the same result. Repeating will not change it: "
    "read the error, look at different code or docs, or try another approach."
)
DENIAL_HINT = (
    "{n} tool calls in a row were denied by policy. Stop trying variants. If the spec blocks the work (file "
    "outside allowed_files, wrong test_command), escalate with a spec_patch option; otherwise use an allowed tool."
)
STOP_HINT = (
    "You are stuck again. End your turn now: escalate with what you tried, or report blocked with the reason."
)


@dataclass
class _Track:
    last: deque[str] = field(default_factory=lambda: deque(maxlen=8))
    denials: int = 0
    stuck: int = 0
    stopped: bool = False


@dataclass(frozen=True)
class StuckSignal:
    kind: str  # repeat · denials
    hint: str
    stop: bool


def _fingerprint(name: str, args_hash: str, result: ToolResult) -> str:
    code = result.error.code if result.error else ""
    raw = f"{name}|{args_hash}|{result.status}|{code}|{result.summary[:200]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class StuckDetector:
    def __init__(self, *, repeat: int = 3, denials: int = 5, stop_after: int = 2) -> None:
        self._repeat = repeat
        self._denials = denials
        self._stop_after = stop_after
        self._tracks: dict[str, _Track] = {}

    def reset(self, key: str) -> None:
        """Đầu mỗi lần chạy agent (rework, model mạnh hơn…): bắt đầu đếm lại."""
        self._tracks.pop(key, None)

    def stopped(self, key: str) -> bool:
        t = self._tracks.get(key)
        return bool(t and t.stopped)

    def observe(self, key: str, name: str, args_hash: str, result: ToolResult) -> StuckSignal | None:
        t = self._tracks.setdefault(key, _Track())
        t.denials = t.denials + 1 if result.status == "denied" else 0
        fp = _fingerprint(name, args_hash, result)
        t.last.append(fp)
        kind: str | None = None
        hint = ""
        if len(t.last) >= self._repeat and len(set(list(t.last)[-self._repeat :])) == 1:
            kind, hint = "repeat", REPEAT_HINT.format(tool=name, n=self._repeat)
        elif t.denials >= self._denials:
            kind, hint = "denials", DENIAL_HINT.format(n=t.denials)
        if kind is None:
            return None
        t.stuck += 1
        t.last.clear()
        t.denials = 0
        stop = t.stuck >= self._stop_after
        if stop:
            t.stopped = True
            hint = f"{hint} {STOP_HINT}"
        return StuckSignal(kind, hint, stop)


def with_signal(result: ToolResult, sig: StuckSignal) -> ToolResult:
    """Gắn gợi ý vào kết quả mà model đọc (summary + structured.stuck)."""
    return result.model_copy(
        update={
            "summary": f"STUCK: {sig.hint}"[:300]
            if sig.stop
            else f"{result.summary} | NOTE: {sig.hint}"[:300],
            "structured": {
                **result.structured,
                "stuck": {"kind": sig.kind, "stop": sig.stop, "hint": sig.hint},
            },
        }
    )


def stopped_result() -> ToolResult:
    return ToolResult.fail("STUCK", STOP_HINT, hint="escalate or report blocked")


__all__ = ["StuckDetector", "StuckSignal", "stopped_result", "with_signal"]

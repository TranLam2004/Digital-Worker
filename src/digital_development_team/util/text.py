from __future__ import annotations

from collections.abc import Sequence

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Ước lượng thô ≈ ký tự / 4. Đủ cho ngưỡng artifactize/memory block. Budget thật dùng usage của provider."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def head_tail(lines: Sequence[str], head: int = 10, tail: int = 10) -> tuple[str, ...]:
    if len(lines) <= head + tail:
        return tuple(lines)
    return (*lines[:head], f"… ({len(lines) - head - tail} lines omitted) …", *lines[-tail:])


def tail(lines: Sequence[str], n: int = 10) -> tuple[str, ...]:
    return tuple(lines[-n:]) if n else ()


def truncate(text: str, max_chars: int, marker: str = "…") -> str:
    return text if len(text) <= max_chars else text[: max(0, max_chars - len(marker))] + marker


def one_line(text: str, max_chars: int = 300) -> str:
    return truncate(" ".join(text.split()), max_chars)

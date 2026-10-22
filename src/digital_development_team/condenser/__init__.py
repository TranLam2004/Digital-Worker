"""Condenser tầng 2 (10-harness/03-condenser): input một lượt vượt evict_at × cửa sổ model → thu gọn tool_result cũ.

Tầng 0–1 (structured + artifactize) đã ở Dispatcher. Tầng 2 chỉ cho API loop (CLI tự quản context của nó):
giữ nguyên brief, mọi lời của model và `keep_last_turns` lượt tool_result gần nhất; tool_result cũ hơn còn
status + summary + artifact ref. Provider giữ lịch sử bất biến (append_only_history) → không đụng.
"""

from __future__ import annotations

import json

from digital_development_team.core.llm import Block, Message, ToolResultBlock

CONDENSED = "older tool output dropped to save context; call the tool again if you need it"
MIN_CHARS = 600  # tool_result ngắn hơn: giữ nguyên, thu gọn không lợi gì


def _short(content: str) -> str:
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        keep = {k: obj[k] for k in ("status", "summary", "artifact_refs") if k in obj}
    else:
        keep = {"summary": content[:200]}
    return json.dumps({**keep, "condensed": CONDENSED}, ensure_ascii=False)


def condense_tool_results(messages: list[Message], *, keep_last_turns: int) -> tuple[list[Message], int]:
    """(messages mới, số tool_result đã thu gọn). Không đổi số message, thứ tự hay tool_use_id."""
    tool_turns = [
        i
        for i, m in enumerate(messages)
        if m.role == "user" and any(isinstance(b, ToolResultBlock) for b in m.blocks)
    ]
    protected = set(tool_turns[-keep_last_turns:]) if keep_last_turns > 0 else set()
    out: list[Message] = []
    n = 0
    for i, m in enumerate(messages):
        if i not in tool_turns or i in protected:
            out.append(m)
            continue
        blocks: list[Block] = []
        for b in m.blocks:
            if isinstance(b, ToolResultBlock) and len(b.content) >= MIN_CHARS and CONDENSED not in b.content:
                blocks.append(b.model_copy(update={"content": _short(b.content)}))
                n += 1
            else:
                blocks.append(b)
        out.append(m.model_copy(update={"blocks": tuple(blocks)}))
    return out, n


__all__ = ["CONDENSED", "condense_tool_results"]

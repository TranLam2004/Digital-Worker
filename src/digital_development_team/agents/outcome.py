"""Tách output cuối của agent (final_message): JSON object · {status: blocked, reason} · văn xuôi (không hợp lệ)."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|yaml|yml)?\s*\n(.*?)```", re.S)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """JSON object đầu tiên hợp lệ trong text: thử nguyên văn → trong code fence → đoạn {…} ngoài cùng."""
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        if not c.startswith("{"):
            continue
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    with contextlib.suppress(Exception):  # yaml lỗi → không phải artifact
        import yaml

        obj = yaml.safe_load(candidates[0]) if candidates[0] else None
        if isinstance(obj, dict):
            return obj
    return None


def blocked_reason(obj: Mapping[str, Any] | None, text: str) -> str | None:
    """{status: blocked, reason} → reason. Không phải → None."""
    if obj and str(obj.get("status", "")).lower() == "blocked":
        return str(obj.get("reason") or "không nêu lý do")[:500]
    low = text.strip().lower()
    if low.startswith("blocked:"):
        return text.strip()[8:].strip()[:500] or "không nêu lý do"
    return None

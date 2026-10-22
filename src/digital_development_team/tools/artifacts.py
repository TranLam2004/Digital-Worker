"""ArtifactStore — output tool lớn → work/<issue>/artifacts/<sha>.<ext>, trả artifact://… + preview (Condenser tầng 1)."""

from __future__ import annotations

import re
from pathlib import Path

from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.hashing import sha256_text
from digital_development_team.util.text import estimate_tokens, head_tail

_REF_RE = re.compile(r"^artifact://(?P<issue>DDT-\d{4,})/(?P<name>[A-Za-z0-9_.-]+)$")


PREVIEW_MAX_LINES = 20  # = ToolResult.preview max_length


class ArtifactStore:
    def __init__(self, layout: HomeLayout) -> None:
        self._layout = layout

    @property
    def layout(self) -> HomeLayout:
        return self._layout

    def put(self, issue: str, text: str, *, ext: str = "txt", label: str | None = None) -> str:
        sha = sha256_text(text).rsplit(":", 1)[-1][:16]
        name = f"{label + '-' if label else ''}{sha}.{ext}"
        path = self._layout.artifacts_dir(issue) / name
        if not path.exists():
            write_text_atomic(path, text, fsync=False)
        return f"artifact://{issue}/{name}"

    def resolve(self, ref: str) -> Path | None:
        m = _REF_RE.match(ref)
        if not m:
            return None
        p = self._layout.artifacts_dir(m["issue"]) / m["name"]
        return p if p.is_file() else None

    def read(self, ref: str) -> str | None:
        p = self.resolve(ref)
        return p.read_text(encoding=ENCODING, errors="replace") if p else None

    @staticmethod
    def preview(text: str, head: int = 10, tail: int = 10) -> tuple[str, ...]:
        """≤ PREVIEW_MAX_LINES dòng kể cả dòng đánh dấu cắt (ToolResult.preview giới hạn 20 — vượt là TOOL_CRASH)."""
        lines = text.splitlines()
        if len(lines) > head + tail:
            head = min(head, PREVIEW_MAX_LINES - 2)
            tail = min(tail, PREVIEW_MAX_LINES - 1 - head)
        return head_tail(lines, head=head, tail=tail)

    @staticmethod
    def should_artifactize(text: str, threshold_tokens: int) -> bool:
        return estimate_tokens(text) > threshold_tokens

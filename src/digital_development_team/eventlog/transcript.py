"""Transcript store — toàn bộ quá trình một lần chạy agent, NGOÀI events.jsonl.

work/<issue>/transcripts/<STAGE>.<attempt>.jsonl — mỗi dòng một bước:
  system      {role, hash, chars, tools[], model}
  brief       {text}                                  ← 4 khối worker nhận
  assistant   {turn, model, stop, text, tool_uses[{id,name,input}], usage{}}
  tool_result {turn, tool_use_id, name, status, summary, error_code, is_error, content≤2000}
  harness     {turn, text}                            ← FINAL_TURN · nudge · schema retry · cảnh báo budget
  outcome     {result, uri, reason, usage{}}

events.jsonl vẫn là audit tối giản (hash, không prompt). Transcript là để NGƯỜI xem "worker đã làm gì",
đã qua redact_secrets (inject từ tầng trên vì eventlog L2 không import policy). Tắt bằng policy.transcripts=false.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING

log = logging.getLogger(__name__)

TRANSCRIPTS_DIR = "transcripts"
CONTENT_PREVIEW = 2000
_NAME_RE = re.compile(r"^(?P<stage>[A-Z_]+)\.(?P<attempt>\d+)\.jsonl$")
Redact = Callable[[str], str]


def transcripts_dir(layout: HomeLayout, issue: str) -> Path:
    return layout.issue_dir(issue) / TRANSCRIPTS_DIR


def transcript_path(layout: HomeLayout, issue: str, stage: str, attempt: int) -> Path:
    return transcripts_dir(layout, issue) / f"{stage}.{attempt}.jsonl"


def _redact_obj(obj: Any, redact: Redact) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v, redact) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v, redact) for v in obj]
    return obj


class TranscriptWriter:
    def __init__(self, path: Path, *, redact: Redact | None = None, clock: Clock | None = None) -> None:
        self._path = path
        self._redact = redact or (lambda s: s)
        self._clock = clock or SystemClock()
        self._seq = 0
        if path.is_file():  # resume sau crash: nối tiếp, đánh dấu
            self._seq = sum(1 for _ in path.open("r", encoding=ENCODING))
            self.write("note", text="resumed — tiếp tục từ checkpoint")

    @classmethod
    def for_run(
        cls,
        layout: HomeLayout,
        issue: str,
        stage: str,
        attempt: int,
        *,
        redact: Redact | None = None,
        clock: Clock | None = None,
    ) -> TranscriptWriter:
        return cls(transcript_path(layout, issue, stage, attempt), redact=redact, clock=clock)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, kind: str, **data: Any) -> None:
        """Append một dòng. Lỗi I/O không được làm hỏng agent loop — chỉ log."""
        entry = {
            "seq": self._seq,
            "ts": self._clock.now().isoformat(),
            "kind": kind,
            **_redact_obj(data, self._redact),
        }
        if isinstance(entry.get("content"), str) and len(entry["content"]) > CONTENT_PREVIEW:
            entry["content_truncated"] = len(entry["content"])
            entry["content"] = entry["content"][:CONTENT_PREVIEW]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding=ENCODING, newline="\n") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._seq += 1
        except OSError as e:  # pragma: no cover
            log.warning("transcript %s: %s", self._path, e)


@dataclass(frozen=True, slots=True)
class TranscriptInfo:
    issue_id: str
    stage: str
    attempt: int
    path: Path
    size: int


def list_transcripts(layout: HomeLayout, issue: str) -> list[TranscriptInfo]:
    d = transcripts_dir(layout, issue)
    if not d.is_dir():
        return []
    out: list[TranscriptInfo] = []
    for p in d.iterdir():
        m = _NAME_RE.match(p.name)
        if m:
            out.append(TranscriptInfo(issue, m["stage"], int(m["attempt"]), p, p.stat().st_size))
    out.sort(key=lambda t: (t.path.stat().st_mtime, t.attempt))
    return out


def read_transcript(path: Path) -> list[dict[str, Any]]:
    """Chịu được dòng cuối ghi dở."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding=ENCODING).split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("%s: bỏ qua dòng hỏng", path)
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out

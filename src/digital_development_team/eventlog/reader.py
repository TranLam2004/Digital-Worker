"""Đọc events.jsonl: một lần hoặc theo dõi (tail -f). Chịu được dòng cuối ghi dở do crash."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from digital_development_team.core.context import CancelToken
from digital_development_team.core.events import Event
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING

log = logging.getLogger(__name__)


def _parse(line: str, *, path: Path, is_last: bool) -> Event | None:
    try:
        return Event.model_validate_json(line)
    except ValidationError as e:
        if is_last:
            log.warning("%s: dòng cuối hỏng (crash giữa chừng?) — bỏ qua", path)
            return None
        raise ValueError(f"{path}: dòng event hỏng giữa file: {e.errors()[0]['msg']}") from e


def iter_events(path: Path) -> Iterator[Event]:
    if not path.is_file():
        return
    lines = path.read_text(encoding=ENCODING).split("\n")
    # phần tử cuối sau split là '' nếu file kết thúc bằng \n đúng chuẩn
    complete = lines[:-1]
    partial = lines[-1]
    for line in complete:
        if line.strip():
            ev = _parse(line, path=path, is_last=False)
            if ev is not None:
                yield ev
    if partial.strip():
        ev = _parse(partial, path=path, is_last=True)
        if ev is not None:
            yield ev


def read_events(layout: HomeLayout, issue: str, *, since: int = -1, until: int | None = None) -> list[Event]:
    """Event có since < seq ≤ until, theo seq tăng dần."""
    out = [
        e
        for e in iter_events(layout.events_path(issue))
        if e.seq > since and (until is None or e.seq <= until)
    ]
    out.sort(key=lambda e: e.seq)
    return out


def follow(
    path: Path,
    *,
    cancel: CancelToken,
    poll_s: float = 0.3,
    from_start: bool = True,
) -> Iterator[Event]:
    """tail -f: yield event mới khi file dài ra. Dừng khi cancel. Chỉ yield dòng đã kết thúc bằng \\n."""
    offset = 0
    if not from_start and path.is_file():
        offset = path.stat().st_size
    buf = ""
    while not cancel.is_cancelled():
        if path.is_file():
            size = path.stat().st_size
            if size < offset:  # file bị cắt/ghi lại → đọc từ đầu
                offset, buf = 0, ""
            if size > offset:
                with path.open("r", encoding=ENCODING, newline="\n") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                buf += chunk
                *done, buf = buf.split("\n")
                for line in done:
                    if line.strip():
                        try:
                            yield Event.model_validate_json(line)
                        except ValidationError:
                            log.warning("%s: bỏ qua dòng hỏng", path)
                continue
        cancel.wait(poll_s)

"""RawItem → Issue (id DDT-xxxx) → item.json (store, actor runner) → queue. Dedupe theo (source, source_id).

blocked_by trong backlog là source_id cùng nguồn → map sang DDT id nếu đã ingest; chưa có → giữ nguyên chuỗi
(runner coi là chưa done → chờ). Event `ingested` do INGEST stage (runner) emit — ingest CLI không ghi events.jsonl.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from digital_development_team.blackboard.store import FileStore
from digital_development_team.core.context import Actor, Clock
from digital_development_team.core.errors import ConfigError
from digital_development_team.core.ids import is_issue_id, next_issue_id
from digital_development_team.core.models import Issue
from digital_development_team.ingest.base import BacklogSource, RawItem
from digital_development_team.ingest.csv_source import CsvSource
from digital_development_team.ingest.json_source import JsonSource
from digital_development_team.ingest.queue import Queue

RUNNER = Actor(role="runner")


@dataclass
class IngestReport:
    source: str
    created: list[str] = field(default_factory=list)  # DDT ids mới
    skipped: list[str] = field(default_factory=list)  # source_id đã có
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.source}: +{len(self.created)} mới · {len(self.skipped)} trùng · {len(self.errors)} lỗi"


def source_for(path: Path, *, source_name: str | None = None) -> BacklogSource:
    ext = path.suffix.lower()
    if ext == ".csv":
        return CsvSource(path, source_name=source_name)
    if ext in (".json", ".jsonl"):
        return JsonSource(path, source_name=source_name)
    raise ConfigError(f"không biết đọc {path.name}", hint="hỗ trợ .csv · .json")


def ingest_items(
    items: Iterable[RawItem], *, store: FileStore, queue: Queue, clock: Clock, source: str
) -> IngestReport:
    rep = IngestReport(source=source)
    last = queue.last_issue_id()
    for raw in items:
        if queue.find_by_source(raw.source, raw.source_id):
            rep.skipped.append(raw.source_id)
            continue
        issue_id = next_issue_id(last)
        blocked = tuple(
            b if is_issue_id(b) else (queue.find_by_source(raw.source, b) or b) for b in raw.blocked_by
        )
        try:
            issue = Issue(
                id=issue_id,
                source=raw.source,
                source_id=raw.source_id,
                title=raw.title,
                body=raw.body,
                labels=raw.labels,
                attachments=raw.attachments,
                priority_hint=raw.priority_hint,
                blocked_by=tuple(b for b in blocked if is_issue_id(b)),
                received_at=clock.now(),
                deadline_at=raw.deadline_at,
                raw={
                    **raw.raw,
                    **(
                        {"blocked_by_unresolved": [b for b in blocked if not is_issue_id(b)]}
                        if any(not is_issue_id(b) for b in blocked)
                        else {}
                    ),
                },
            )
            store.write(issue_id, "item.json", issue.model_dump(mode="json"), actor=RUNNER)
            queue.enqueue(issue)
        except Exception as e:
            rep.errors.append(f"{raw.source_id}: {e}")
            continue
        last = issue_id
        rep.created.append(issue_id)
    return rep


def ingest_file(
    path: Path, *, store: FileStore, queue: Queue, clock: Clock, source_name: str | None = None
) -> IngestReport:
    src = source_for(path, source_name=source_name)
    return ingest_items(src.items(), store=store, queue=queue, clock=clock, source=src.name)

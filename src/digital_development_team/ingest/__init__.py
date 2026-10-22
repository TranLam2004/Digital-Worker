"""Ingest — backlog → item.json + queue. 0 LLM, chỗ không được phép sai.

    n = ingest_file(svc, Path("backlog.csv"))     # CSV hoặc JSON → Issue → store.write(item.json) + queue
    q = Queue(db); q.next()                        # issue kế theo ưu tiên, bỏ qua blocked_by chưa done

Nguồn: csv_source · json_source (P1). github/jira: TODO(P3-#37) — cùng Protocol BacklogSource.
"""

from __future__ import annotations

from digital_development_team.ingest.base import BacklogSource, RawItem
from digital_development_team.ingest.pipeline import IngestReport, ingest_file, ingest_items
from digital_development_team.ingest.queue import Queue, QueueRow

__all__ = ["BacklogSource", "IngestReport", "Queue", "QueueRow", "RawItem", "ingest_file", "ingest_items"]

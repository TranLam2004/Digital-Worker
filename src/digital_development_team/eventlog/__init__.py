"""Event log append-only: work/<issue>/events.jsonl + state/events.global.jsonl.

- Chỉ process runner ghi (ADR). CLI/web ghi gate_requests SQLite, runner thấy mới emit.
- seq tăng dần per issue (counter SQLite), fsync mỗi dòng. Replay dùng seq, không dùng ts.
- Không ghi secret, prompt đầy đủ, tool result đầy đủ — chỉ hash + artifact_ref.
"""

from __future__ import annotations

from digital_development_team.eventlog.memory import InMemorySink
from digital_development_team.eventlog.reader import follow, iter_events, read_events
from digital_development_team.eventlog.replay import format_event, replay
from digital_development_team.eventlog.writer import JsonlEventSink

__all__ = ["InMemorySink", "JsonlEventSink", "follow", "format_event", "iter_events", "read_events", "replay"]

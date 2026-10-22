"""InMemorySink — EventSink cho test và dry-run. Cùng hợp đồng seq với JsonlEventSink."""

from __future__ import annotations

from collections import defaultdict

from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.core.enums import EventType
from digital_development_team.core.events import Event, EventDraft


class InMemorySink:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._seq: dict[str, int] = defaultdict(int)
        self.events: list[Event] = []

    def emit(self, draft: EventDraft) -> Event:
        seq = self._seq[draft.issue_id]
        self._seq[draft.issue_id] = seq + 1
        ev = Event.from_draft(draft, seq=seq, ts=self._clock.now())
        self.events.append(ev)
        return ev

    def by_issue(self, issue: str) -> list[Event]:
        return [e for e in self.events if e.issue_id == issue]

    def of_type(self, *types: EventType) -> list[Event]:
        return [e for e in self.events if e.event in types]

    def types(self, issue: str | None = None) -> list[str]:
        return [e.event.value for e in (self.by_issue(issue) if issue else self.events)]

    def clear(self) -> None:
        self.events.clear()
        self._seq.clear()

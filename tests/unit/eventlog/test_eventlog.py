from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import SchemaError
from digital_development_team.core.events import EventDraft, LlmInfo
from digital_development_team.eventlog import (
    InMemorySink,
    JsonlEventSink,
    follow,
    format_event,
    iter_events,
    read_events,
    replay,
)
from digital_development_team.schema import SchemaRegistry
from digital_development_team.storage import Database, HomeLayout


def _draft(issue: str = "DDT-0001", event: EventType = EventType.STAGE_ENTER, **kw: object) -> EventDraft:
    return EventDraft(issue_id=issue, actor="runner", event=event, stage=Stage.TRIAGE, **kw)  # type: ignore[arg-type]


@pytest.fixture
def sink(tmp_home: Path, fake_clock: FakeClock) -> JsonlEventSink:
    lay = HomeLayout(tmp_home).ensure()
    reg = SchemaRegistry()
    return JsonlEventSink(
        lay, Database(lay.db_path), clock=fake_clock, validate=lambda d: reg.raise_if_invalid("event", d)
    )


def test_emit_writes_valid_lines_with_contiguous_seq(sink: JsonlEventSink, tmp_home: Path) -> None:
    reg = SchemaRegistry()
    for i in range(5):
        ev = sink.emit(_draft(data={"i": i}))
        assert ev.seq == i
    sink.emit(_draft(issue="global", event=EventType.STOP_REQUESTED))
    lines = (tmp_home / "work" / "DDT-0001" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    for line in lines:
        assert reg.is_valid("event", json.loads(line))
    assert (tmp_home / "state" / "events.global.jsonl").is_file()
    evs = read_events(HomeLayout(tmp_home), "DDT-0001", since=1, until=3)
    assert [e.seq for e in evs] == [2, 3]


def test_invalid_event_rejected_before_seq(sink: JsonlEventSink) -> None:
    # issue_id sai pattern → schema fail → không cấp seq
    with pytest.raises(SchemaError):
        sink.emit(EventDraft(issue_id="BAD", actor="runner", event=EventType.STAGE_ENTER))
    ev = sink.emit(_draft())
    assert ev.seq == 0


def test_emit_threadsafe(sink: JsonlEventSink, tmp_home: Path) -> None:
    def w() -> None:
        for _ in range(20):
            sink.emit(_draft())

    ts = [threading.Thread(target=w) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    evs = list(iter_events(HomeLayout(tmp_home).events_path("DDT-0001")))
    assert sorted(e.seq for e in evs) == list(range(80))


def test_listener_errors_do_not_break_emit(sink: JsonlEventSink) -> None:
    seen: list[int] = []
    sink.add_listener(lambda e: seen.append(e.seq))
    sink.add_listener(lambda e: 1 / 0)
    sink.emit(_draft())
    assert seen == [0]


def test_reader_tolerates_partial_last_line(tmp_path: Path, sink: JsonlEventSink, tmp_home: Path) -> None:
    sink.emit(_draft())
    sink.emit(_draft())
    p = HomeLayout(tmp_home).events_path("DDT-0001")
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write('{"ts": "2026-10-03T09:00:00Z", "seq": 2, "iss')  # crash giữa chừng
    evs = list(iter_events(p))
    assert [e.seq for e in evs] == [0, 1]


def test_reader_raises_on_corrupt_middle_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text('{"bad": 1}\n{"also": 2}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="hỏng giữa file"):
        list(iter_events(p))


def test_follow_yields_new_events(sink: JsonlEventSink, tmp_home: Path) -> None:
    p = HomeLayout(tmp_home).events_path("DDT-0001")
    sink.emit(_draft())
    cancel = CancelToken()
    got: list[int] = []

    def consume() -> None:
        for ev in follow(p, cancel=cancel, poll_s=0.02):
            got.append(ev.seq)
            if len(got) == 3:
                cancel.cancel()

    t = threading.Thread(target=consume)
    t.start()
    sink.emit(_draft())
    sink.emit(_draft())
    t.join(timeout=5)
    assert got == [0, 1, 2]


def test_inmemory_sink_and_replay(fake_clock: FakeClock) -> None:
    s = InMemorySink(fake_clock)
    s.emit(
        _draft(event=EventType.SLOT_WRITTEN, data={"slot": "intake.yaml", "version": 1}, hash="ab12cd34ef")
    )
    s.emit(
        _draft(event=EventType.LLM_CALL, llm=LlmInfo(model="mock", tokens_in=10, tokens_out=5, latency_ms=3))
    )
    s.emit(
        _draft(
            event=EventType.SLOT_REJECTED,
            data={"slot": "spec.yaml", "errors": [{"path": "goal", "msg": "required"}]},
        )
    )
    s.emit(_draft(issue="DDT-0002"))
    assert s.types("DDT-0001") == ["slot_written", "llm_call", "slot_rejected"]
    assert [e.seq for e in s.by_issue("DDT-0002")] == [0]
    lines = list(replay(s.by_issue("DDT-0001"), until=1))
    assert len(lines) == 2
    assert "intake.yaml v1 → sha:ab12cd34" in lines[0]
    assert "mock in=10 out=5" in lines[1]
    assert "spec.yaml ✗ 1 lỗi: goal required" in format_event(s.by_issue("DDT-0001")[2])

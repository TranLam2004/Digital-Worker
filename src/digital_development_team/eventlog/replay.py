"""Replay = xem lại, không chạy lại tool. Mỗi event → 1 dòng người đọc được.

`seq 3 · intake · slot_written intake.yaml v1 → sha:ab12cd34`
Dựng lại checkpoint từ events (--resume): TODO(P1-B12) runner/recover.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from digital_development_team.core.enums import EventType
from digital_development_team.core.events import Event
from digital_development_team.util.hashing import short

_SLOT_EVENTS = {EventType.SLOT_WRITTEN, EventType.SLOT_REJECTED, EventType.SLOT_VERSIONED}


def _detail(ev: Event) -> str:
    d = ev.data
    t = ev.event
    if t in _SLOT_EVENTS:
        slot = d.get("slot", "?")
        if t is EventType.SLOT_WRITTEN:
            return f"{slot} v{d.get('version', '?')} → sha:{short(ev.hash) if ev.hash else '?'}"
        if t is EventType.SLOT_VERSIONED:
            return f"{slot} v{d.get('from')} → v{d.get('to')}"
        errs = d.get("errors", [])
        first = errs[0] if errs else {}
        return f"{slot} ✗ {len(errs)} lỗi: {first.get('path', '')} {first.get('msg', '')}".rstrip()
    if t is EventType.LLM_CALL and ev.llm:
        return f"{ev.llm.model} in={ev.llm.tokens_in} out={ev.llm.tokens_out} {ev.llm.latency_ms}ms"
    if t in (EventType.TOOL_CALL, EventType.TOOL_DENIED) and ev.tool:
        extra = ev.tool.denied_reason or ev.tool.approved_by or ""
        target = d.get("path") or d.get("cmd") or ""
        return (
            f"{ev.tool.name} {target} ({ev.tool.status or 'denied'}{', ' + extra if extra else ''})".replace(
                "  ", " "
            )
        )
    if t in (EventType.GATE_WAIT, EventType.GATE_RESOLVED, EventType.GATE_TIMEOUT) and ev.human:
        wait = f" (chờ {ev.human.wait_sec}s)" if ev.human.wait_sec is not None else ""
        return f"{ev.human.gate or ''} {ev.human.choice or ''}{wait}".strip()
    if t in (EventType.STAGE_ENTER, EventType.STAGE_EXIT):
        return " ".join(
            f"{k}={v}" for k, v in d.items() if k in ("result", "to", "tier", "reason", "duration_ms")
        )
    if d:
        items = [f"{k}={v}" for k, v in d.items() if isinstance(v, (str, int, float, bool))]
        return " ".join(items)[:120]
    return ""


def format_event(ev: Event) -> str:
    stage = f" [{ev.stage.value}]" if ev.stage else ""
    detail = _detail(ev)
    return f"seq {ev.seq:>3} · {ev.ts.strftime('%H:%M:%S')} · {ev.actor:<10} · {ev.event.value}{stage} {detail}".rstrip()


def replay(events: Iterable[Event], *, until: int | None = None) -> Iterator[str]:
    for ev in sorted(events, key=lambda e: e.seq):
        if until is not None and ev.seq > until:
            break
        yield format_event(ev)

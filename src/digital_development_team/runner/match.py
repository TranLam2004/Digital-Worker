"""Vòng đời một item thi đấu (0 LLM) — Start Working · match.json · dựng answer theo submit template · Submit.

Gọi từ INGEST (start working: lấy MVS + template sớm cho SPEC/Verifier) và MERGE (push + submit). Lỗi Game Master
không làm hỏng pipeline: ghi vào match.json, MatchSync nộp lại sau (cùng Idempotency-Key → không thành nộp 2 lần).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Mapping
from typing import Any

from digital_development_team.core.enums import EventType
from digital_development_team.core.errors import ConfigError, DDTError, MatchDeferred, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.gamemaster.models import ItemStatus, MatchStatus
from digital_development_team.runner.context import RUNNER_ACTOR, Services
from digital_development_team.storage.submissions import SubmissionLedger
from digital_development_team.util.hashing import sha256_json

log = logging.getLogger(__name__)
SLOT = "match.json"
_MATCH_LOCK = threading.RLock()


def contest_item(svc: Services, issue: str) -> dict[str, Any] | None:
    """item.json của issue đến từ Game Master → raw (item_id, item_type, match_id…); khác → None."""
    try:
        item = svc.store.read(issue, "item.json").content
    except StoreError:
        return None
    raw = item.get("raw") or {}
    return dict(raw) if item.get("source") == "gamemaster" and raw.get("item_id") else None


def read_match(svc: Services, issue: str) -> dict[str, Any]:
    try:
        result = dict(svc.store.read(issue, SLOT).content)
    except StoreError:
        return {}
    persisted = SubmissionLedger(svc.db).get(str(result.get("match_id", "")), str(result.get("item_id", "")))
    if persisted:
        result["submission"] = persisted
    return result


def write_match(svc: Services, issue: str, updates: Mapping[str, Any]) -> dict[str, Any]:
    # Scheduler, worker and grader can update the same slot concurrently.
    with _MATCH_LOCK:
        return _write_match(svc, issue, updates)


def _write_match(svc: Services, issue: str, updates: Mapping[str, Any]) -> dict[str, Any]:
    cur = read_match(svc, issue)
    if not cur:
        raw = contest_item(svc, issue) or {}
        cur = {
            "issue_id": issue,
            "match_id": str(raw.get("match_id", "")),
            "item_id": str(raw.get("item_id", "")),
            "type": str(raw.get("item_type", "TASK")),
            "points": raw.get("points"),
            "revealed_at": raw.get("revealed_at"),
            "started": False,
        }
        deadline = _item_deadline(svc, issue)
        if deadline:
            cur["deadline_at"] = deadline
    new = {**cur, **updates}
    try:
        stored = svc.store.read(issue, SLOT).content
    except StoreError:
        stored = {}
    if new != stored:
        svc.store.write(issue, SLOT, new, actor=RUNNER_ACTOR)
    return new


def _item_deadline(svc: Services, issue: str) -> str | None:
    try:
        v = svc.store.read(issue, "item.json").content.get("deadline_at")
    except StoreError:
        return None
    return str(v) if v else None


def _emit(svc: Services, issue: str, event: EventType, data: dict[str, Any]) -> None:
    svc.sink.emit(EventDraft(issue_id=issue, actor="runner", event=event, data=data))


# ───── Start Working ─────
def require_active(svc: Services, issue: str) -> None:
    """Re-read the authorized channel before a contest mutation; fail closed."""
    raw = contest_item(svc, issue)
    if raw is None:
        return
    if svc.gamemaster is None:
        raise ConfigError("Contest issue requires a Game Player adapter")
    try:
        snap = svc.gamemaster.snapshot()
    except DDTError as e:
        raise MatchDeferred(f"Cannot confirm live match state: {e.message}") from e
    item = snap.item(str(raw["item_id"]))
    if snap.match_id != str(raw.get("match_id")) or snap.status is not MatchStatus.IN_PROGRESS:
        raise MatchDeferred(f"Match is not active: {snap.status}")
    if item is None or item.status is ItemStatus.CLOSED:
        raise MatchDeferred("Item is absent or closed")
    from digital_development_team.ingest.gamemaster_source import fingerprint

    if (
        raw.get("fingerprint")
        and fingerprint(item) != raw["fingerprint"]
        and not SubmissionLedger(svc.db).get(snap.match_id, item.id)
    ):
        raise MatchDeferred("Item changed; wait for synchronization of its new version")
    if (item.deadline_at and snap.server_time >= item.deadline_at) or (
        snap.ends_at and snap.server_time >= snap.ends_at
    ):
        raise MatchDeferred("Server deadline has elapsed")


def start_working(svc: Services, issue: str) -> dict[str, Any] | None:
    """Báo Game Master bắt đầu làm item → submit template (+ MVS). Gọi lại an toàn (server trả cùng thông tin).
    Lỗi mạng: giữ checkpoint INGEST, không chạy agent khi chưa có template/MVS."""
    raw = contest_item(svc, issue)
    if raw is None or svc.gamemaster is None:
        return None
    existing = read_match(svc, issue)
    if existing.get("started"):
        return existing
    item_id = str(raw["item_id"])
    try:
        require_active(svc, issue)
        res = svc.gamemaster.start_working(item_id)
        if not res.submit_template:
            raise ConfigError("Start Working returned an empty submit template")
        if raw.get("item_type") in ("BUG", "CR") and not res.mvs:
            raise ConfigError("BUG/CR requires MVS from Start Working")
    except DDTError as e:
        log.warning("%s: start working %s lỗi: %s", issue, item_id, e.message)
        write_match(svc, issue, {"started": False, "start_error": e.message[:300]})
        raise MatchDeferred(f"Start Working incomplete: {e.message}") from e
    m = write_match(
        svc,
        issue,
        {
            "started": True,
            "submit_template": res.submit_template,
            "mvs": [{**m, "id": str(m.get("id") or f"MVS-{i}")} for i, m in enumerate(res.mvs, 1)],
            "start_error": None,
        },
    )
    _emit(svc, issue, EventType.MATCH_STARTED, {"item": item_id, "mvs": len(res.mvs)})
    return m


# ───── answer theo template ─────
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(root.?cause|cause|analysis|investigation|diagnos)"), "root_cause"),
    (re.compile(r"(?i)(evidence|test|verif|mvs|scenario|result)"), "evidence"),
    (re.compile(r"(?i)(file|changed|diff)"), "files"),
    (re.compile(r"(?i)(commit|revision|sha)"), "sha"),
    (re.compile(r"(?i)(risk|impact|regression)"), "impact"),
    (re.compile(r"(?i)(summary|description|solution|resolution|change|fix|note|answer|detail)"), "summary"),
)


def _facts(svc: Services, issue: str, sha: str) -> dict[str, Any]:
    def slot(name: str) -> dict[str, Any]:
        try:
            return dict(svc.store.read(issue, name).content)
        except StoreError:
            return {}

    spec, evidence, patch, answer = (
        slot("spec.yaml"),
        slot("evidence.json"),
        slot("patch/"),
        slot("answer.yaml"),
    )
    impl = str(patch.get("impl_log") or answer.get("answer") or "").strip()
    criteria = [
        f"{c.get('id', '?')} {c.get('result', '?')}: {str(c.get('evidence') or c.get('text') or '')[:200]}"
        for c in evidence.get("criteria") or []
        if isinstance(c, dict)
    ]
    reg = evidence.get("regression") or {}
    ev_lines = [f"Verifier verdict: {evidence.get('verdict', 'n/a')}", *criteria]
    if isinstance(reg, dict) and reg:
        ev_lines.append(f"Regression `{spec.get('regression_command', '')}`: {reg.get('result', '?')}")
    if spec.get("test_command"):
        ev_lines.append(f"Test command: `{spec['test_command']}`")
    goal = str(spec.get("goal") or "")
    return {
        "summary": (goal + ("\n\n" + impl if impl else "")).strip() or impl or goal,
        "root_cause": impl or goal,
        "evidence": "\n".join(ev_lines),
        "files": list(patch.get("files_touched") or []),
        "sha": sha,
        "impact": f"Regression: {reg.get('result', 'n/a') if isinstance(reg, dict) else 'n/a'}",
    }


def build_answer(template: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    """Điền submit template bằng dữ liệu đã có (spec · evidence · patch · answer) — 0 LLM, tất định.
    Unknown or typed placeholders require an explicit mapping; never copy sample values."""

    def value_for(key: str, sample: Any) -> Any:
        kind = next((k for rx, k in _RULES if rx.search(key)), None)
        if isinstance(sample, Mapping):
            return build_answer(sample, facts)
        if (
            kind is None
            or isinstance(sample, int | float | bool)
            or (isinstance(sample, list) and any(isinstance(x, Mapping | list) for x in sample))
        ):
            raise ConfigError(f"Submit template field needs an explicit adapter mapping: {key}")
        v = facts.get(kind, "")
        if isinstance(sample, list):
            return list(v) if isinstance(v, list) else [x for x in str(v).splitlines() if x.strip()]
        return ", ".join(v) if isinstance(v, list) else str(v)

    if not template:
        raise ConfigError("Cannot submit without the organizer's answer template")
    return {k: value_for(str(k), v) for k, v in template.items()}


def idempotency_key(match_id: str, item_id: str, sha: str) -> str:
    return hashlib.sha256(f"{match_id}|{item_id}|{sha}".encode()).hexdigest()[:40]


# ───── Submit ─────
def submit(svc: Services, issue: str, sha: str) -> dict[str, Any] | None:
    """Nộp item một lần. Body + key ghi vào match.json TRƯỚC khi gọi → retry (MatchSync) gửi y hệt."""
    raw = contest_item(svc, issue)
    if raw is None or svc.gamemaster is None:
        return None
    m = read_match(svc, issue)
    item_id, match_id = str(raw["item_id"]), str(raw.get("match_id", ""))
    ledger = SubmissionLedger(svc.db)
    sub = ledger.get(match_id, item_id)
    # Adopt a pre-migration request, including an ambiguous timeout, without changing it.
    old = dict(m.get("submission") or {})
    if sub is None and old.get("idempotency_key"):
        ledger.reserve(
            match_id, item_id, issue, {k: old[k] for k in ("git_revision", "answer", "idempotency_key")}
        )
        sub = ledger.record(match_id, item_id, old)
    if sub and sub.get("status") in ("ACCEPTED", "REJECTED"):
        write_match(svc, issue, {"submission": sub})
        return sub
    require_active(svc, issue)
    if not m.get("started"):
        m = start_working(svc, issue) or m
    if not m.get("started"):
        raise MatchDeferred("Start Working must complete before submit")
    if sub is None:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ConfigError("gitRevision must be a full 40-character commit SHA")
        verified = m.get("verification") or {}
        if verified.get("status") != "pass" or verified.get("git_revision") != sha:
            raise ConfigError("Submission requires passing final-tree verification for this SHA")
        for slot, field in (("spec.yaml", "spec_hash"), ("evidence.json", "evidence_hash")):
            if sha256_json(svc.store.read(issue, slot).content) != verified.get(field):
                raise ConfigError(f"{slot} changed after final-tree verification")
        sub = ledger.reserve(
            match_id,
            item_id,
            issue,
            {
                "git_revision": sha,
                "answer": build_answer(dict(m.get("submit_template") or {}), _facts(svc, issue, sha)),
                "idempotency_key": idempotency_key(match_id, item_id, sha),
            },
        )
    write_match(svc, issue, {"submission": sub})
    try:
        res = svc.gamemaster.submit(
            item_id,
            git_revision=sub["git_revision"],
            answer=sub["answer"],
            idempotency_key=sub["idempotency_key"],
        )
    except DDTError as e:
        status = getattr(e, "status", None)
        sub = ledger.record(
            match_id, item_id, {"status": "UNKNOWN", "error": e.message[:300], "http_status": status}
        )
        write_match(svc, issue, {"submission": sub})
        _emit(
            svc,
            issue,
            EventType.MATCH_SUBMITTED,
            {"item": item_id, "status": "ERROR", "error": e.message[:200]},
        )
        return sub
    sub = ledger.record(
        match_id,
        item_id,
        {
            "status": res.status,
            "submission_id": res.submission_id,
            "provisional_score": res.provisional_score,
            "grade": "PENDING" if res.accepted else None,
            "error": None if res.accepted else res.message[:300],
        },
    )
    write_match(svc, issue, {"submission": sub})
    _emit(
        svc,
        issue,
        EventType.MATCH_SUBMITTED,
        {"item": item_id, "status": res.status, "submission": res.submission_id, "sha": sha[:12]},
    )
    return sub


def needs_submit_retry(svc: Services, issue: str) -> bool:
    sub = read_match(svc, issue).get("submission") or {}
    return (
        bool(sub.get("git_revision"))
        and sub.get("status") in (None, "ERROR", "UNKNOWN")
        and sub.get("http_status") not in (409, 422)
    )


def poll_grade(svc: Services, issue: str) -> str | None:
    """PENDING → hỏi Grader. Trả trạng thái mới khi vừa đổi (PASSED/FAILED), None nếu chưa đổi."""
    if svc.gamemaster is None:
        return None
    m = read_match(svc, issue)
    sub = dict(m.get("submission") or {})
    if sub.get("grade") != "PENDING" or not sub.get("submission_id"):
        return None
    try:
        g = svc.gamemaster.submission(str(m.get("item_id")), str(sub["submission_id"]))
    except DDTError as e:
        log.warning("%s: hỏi kết quả chấm lỗi: %s", issue, e.message)
        return None
    if g.status.value == "PENDING":
        return None
    ledger = SubmissionLedger(svc.db)
    if ledger.get(str(m.get("match_id")), str(m.get("item_id"))):
        sub = ledger.record(
            str(m.get("match_id")), str(m.get("item_id")), {"grade": g.status.value, "score": g.score}
        )
    else:
        sub.update({"grade": g.status.value, "score": g.score})
    write_match(svc, issue, {"submission": sub})
    _emit(
        svc,
        issue,
        EventType.MATCH_GRADED,
        {"item": m.get("item_id"), "grade": g.status.value, "score": g.score},
    )
    return g.status.value

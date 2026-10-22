"""Kết quả issue → tín hiệu học hỏi tất định (0 LLM): episode · role_outcomes · worker_stats · standup.

Runner gọi `record_issue_outcome` khi issue vào DONE hoặc BLOCKED. Idempotent theo issue_id (resume, chạy lại an toàn).
`ddt perf --backfill` ghi cho issue cũ với agent_version 0 (không rõ JD nào đã chạy).
Định nghĩa "pass" theo role (50-knowledge/05-profiles), đo được từ checkpoint + events + artifact:

  intake    tier ước lượng lệch tier cuối không quá 1 bậc, và G0 không bị từ chối
  planner   không re-localize và issue tới được IMPLEMENT
  analyst   Verifier không chấm AC nào là unclear
  dev       pass@1: issue xong mà không rework
  verifier  người không từ chối ở G1 (chấm pass mà người thấy sai = Verifier sai)
  security  người không từ chối vì lý do bảo mật
  leader    người không override quyết định merge của Leader

`familiarity(db, module)` cấp tín hiệu cho confidence: tỷ lệ pass của Dev trên module khi đã có ≥ 2 lần.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from digital_development_team.core.enums import EventType
from digital_development_team.core.errors import DDTError
from digital_development_team.core.events import Event
from digital_development_team.core.models import Checkpoint
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING

STAGE_ROLE: dict[str, str] = {
    "TRIAGE": "intake",
    "LOCALIZE": "planner",
    "SPEC": "analyst",
    "IMPLEMENT": "dev",
    "VERIFY": "verifier",
    "REVIEW": "security",
    "GATE": "leader",
}
TIER_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
SECURITY_WORDS = ("bảo mật", "security", "secret", "injection", "xss", "sqli", "cve")
MIN_ATTEMPTS_FOR_FAMILIARITY = 2
OPEN_END = 10**6
SlotReader = Callable[[str], Mapping[str, Any]]


@dataclass
class RoleOutcome:
    role: str
    passed: bool | None
    reason: str
    tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    model: str | None = None


@dataclass
class RecordResult:
    issue_id: str
    seq_no: int
    outcome: str
    roles: dict[str, RoleOutcome] = field(default_factory=dict)
    standup_line: str = ""


def _role(actor: str) -> str:
    return actor.split("/", 1)[0].split(":", 1)[0]


def _facts(cp: Checkpoint, key: str) -> dict[str, Any]:
    v = cp.facts.get(key)
    return dict(v) if isinstance(v, dict) else {}


def _art(cp: Checkpoint, read_slot: SlotReader, slot: str, key: str) -> dict[str, Any]:
    """Artifact trên blackboard (nguồn thật), rơi về bản tóm tắt trong checkpoint.facts nếu chưa có."""
    content = read_slot(slot)
    return dict(content) if content else _facts(cp, key)


def judge_roles(cp: Checkpoint, events: Sequence[Event], read_slot: SlotReader) -> dict[str, RoleOutcome]:
    """Ai đã tham gia và pass hay không, theo định nghĩa từng role. Không ghi gì."""
    outcome = "done" if cp.status.value == "done" else "blocked"
    entered = {e.stage.value for e in events if e.event is EventType.STAGE_ENTER and e.stage}
    usage: dict[str, RoleOutcome] = {}
    for e in events:
        if e.event is EventType.LLM_CALL and e.llm:
            r = usage.setdefault(_role(e.actor), RoleOutcome(_role(e.actor), None, ""))
            r.tokens += e.llm.tokens_in + e.llm.tokens_out
            r.cost_usd += e.llm.cost_usd
            r.calls += 1
            r.model = e.llm.model
    g1 = [
        e.human
        for e in events
        if e.event in (EventType.GATE_RESOLVED, EventType.GATE_TIMEOUT) and e.human and e.human.gate == "G1"
    ]
    g0 = [
        e.human
        for e in events
        if e.event in (EventType.GATE_RESOLVED, EventType.GATE_TIMEOUT) and e.human and e.human.gate == "G0"
    ]
    g1_reject = [h for h in g1 if str(h.choice).startswith("reject")]
    g1_approve = [h for h in g1 if h.choice == "approve"]
    blocked_from = next(
        (str(e.data.get("from")) for e in reversed(events) if e.event is EventType.BLOCKED), None
    )
    intake = _art(cp, read_slot, "intake.yaml", "intake")
    rule_only = bool(cp.facts.get("gate_rule_only"))
    out: dict[str, RoleOutcome] = {}

    def put(role: str, passed: bool | None, reason: str) -> None:
        base = usage.get(role) or RoleOutcome(role, None, "")
        out[role] = RoleOutcome(
            role, passed, reason, base.tokens, round(base.cost_usd, 6), base.calls, base.model
        )

    if "TRIAGE" in entered or "intake" in usage:
        est = str(intake.get("risk_tier") or "")
        if any(str(h.choice) == "reject" for h in g0):
            put("intake", False, "G0 bị người từ chối")
        elif est in TIER_RANK:
            gap = TIER_RANK.get(cp.tier.value, 0) - TIER_RANK[est]
            put("intake", gap <= 1, f"ước lượng {est}, tier cuối {cp.tier.value}")
        else:
            put("intake", None, "không có risk_tier")
    if "LOCALIZE" in entered:
        if cp.reloc > 0:
            put("planner", False, f"re-localize {cp.reloc} lần")
        elif "IMPLEMENT" in entered:
            put("planner", True, "Dev dùng candidate đầu")
        else:
            put("planner", False if blocked_from == "LOCALIZE" else None, "chưa tới IMPLEMENT")
    if "SPEC" in entered:
        ev = read_slot("evidence.json")
        crit = [c for c in ev.get("criteria", []) if isinstance(c, Mapping)]
        if not crit:
            put("analyst", None, "chưa có evidence")
        else:
            unclear = [str(c.get("id")) for c in crit if c.get("result") == "unclear"]
            put(
                "analyst", not unclear, f"AC unclear: {', '.join(unclear)}" if unclear else "mọi AC kiểm được"
            )
    if "IMPLEMENT" in entered:
        if outcome == "done" and cp.rework == 0:
            put("dev", True, "xong không rework")
        else:
            put("dev", False, f"rework {cp.rework}" if cp.rework else f"bị chặn ở {blocked_from or '?'}")
    elif "ANSWER" in entered:  # câu hỏi: Dev trả lời, không sửa code
        put("dev", outcome == "done", "đã trả lời câu hỏi" if outcome == "done" else "câu trả lời bị chặn")
    canary = _facts(cp, "canary")
    if "VERIFY" in entered and canary.get("shown") and not canary.get("caught"):
        put("verifier", False, f"bỏ lọt canary {canary.get('mutation')}")
    elif "VERIFY" in entered:
        if g1_reject:
            put("verifier", False, "người từ chối ở G1")
        elif g1_approve or outcome == "done":
            put("verifier", True, "người không bác kết luận")
        else:
            put("verifier", None, "issue dừng trước khi người xét")
    if "security" in usage or read_slot("sec_report.json"):
        notes = " ".join(str(h.note or "").lower() for h in g1_reject)
        if any(w in notes for w in SECURITY_WORDS):
            put("security", False, "người từ chối vì bảo mật")
        elif outcome == "done":
            put("security", True, "không bị bác về bảo mật")
        else:
            put("security", None, "issue dừng trước khi người xét")
    if "GATE" in entered and not rule_only:
        action = str(_art(cp, read_slot, "decision.json", "decision").get("action") or "")
        if action == "merge" and g1_reject:
            put("leader", False, "người override quyết định merge")
        elif g1_approve or outcome == "done":
            put("leader", True, "người không override")
        else:
            put("leader", None, f"action {action or '?'}")
    return out


def _seq_next(db: Database) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(seq_no), 0) FROM role_outcomes") or 0) + 1


def already_recorded(db: Database, issue: str) -> bool:
    return db.one("SELECT 1 FROM role_outcomes WHERE issue_id=? LIMIT 1", (issue,)) is not None


def record_issue_outcome(
    *,
    db: Database,
    layout: HomeLayout,
    checkpoint: Checkpoint,
    events: Sequence[Event],
    read_slot: SlotReader,
    agent_versions: Mapping[str, int],
    now: datetime,
    unknown_version: bool = False,
    no_learn: bool = False,
) -> RecordResult | None:
    """Ghi một lần cho mỗi issue. Trả None nếu đã ghi rồi (idempotent)."""
    cp = checkpoint
    issue = cp.issue_id
    if already_recorded(db, issue):
        return None
    outcome = "done" if cp.status.value == "done" else "blocked"
    intake = _art(cp, read_slot, "intake.yaml", "intake")
    decision = _art(cp, read_slot, "decision.json", "decision")
    itype = str(intake.get("type") or "unknown")
    module = str(intake.get("affected_area") or "unknown")
    roles = judge_roles(cp, events, read_slot)
    brief_usage = _facts(cp, "brief_usage")
    if not roles:  # issue chưa qua agent nào (vd blocked lúc INGEST) → vẫn ghi episode, không có role
        roles = {}
    seq = _seq_next(db)
    patch = read_slot("patch/")
    files = (
        [str(f) for f in patch.get("files_touched", [])]
        if isinstance(patch.get("files_touched"), list)
        else []
    )
    blocked_reason = next(
        (str(e.data.get("reason")) for e in reversed(events) if e.event is EventType.BLOCKED), None
    )
    rework_reason = next(
        (str(e.data.get("reason")) for e in reversed(events) if e.event is EventType.REWORK), None
    )
    human_fix = any(
        (e.event is EventType.GATE_RESOLVED and e.human and str(e.human.choice).startswith("reject"))
        or e.event is EventType.HUMAN_OVERRIDE  # người chọn khác khuyến nghị của agent
        for e in events
    )
    confidence = cp.facts.get("confidence")
    ts = now.isoformat()
    with db.transaction() as c:
        for r in roles.values():
            c.execute(
                "INSERT INTO role_outcomes(issue_id, role, agent_version, issue_type, module, tier, outcome, passed, reason,"
                " tokens, cost_usd, calls, model, seq_no, finished_at, learned_skills, memory_refs, no_learn)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    issue,
                    r.role,
                    0 if unknown_version else int(agent_versions.get(r.role, 1)),
                    itype,
                    module,
                    cp.tier.value,
                    outcome,
                    None if r.passed is None else int(r.passed),
                    r.reason,
                    r.tokens,
                    r.cost_usd,
                    r.calls,
                    r.model,
                    seq,
                    ts,
                    int((brief_usage.get(r.role) or {}).get("learned_skills", 0)),
                    int((brief_usage.get(r.role) or {}).get("memory_refs", 0)),
                    int(no_learn),
                ),
            )
        if not roles:
            c.execute(
                "INSERT INTO role_outcomes(issue_id, role, agent_version, issue_type, module, tier, outcome, passed, reason,"
                " tokens, cost_usd, calls, model, seq_no, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    issue,
                    "runner",
                    1,
                    itype,
                    module,
                    cp.tier.value,
                    outcome,
                    None,
                    "không có agent nào chạy",
                    0,
                    0.0,
                    0,
                    None,
                    seq,
                    ts,
                ),
            )
        c.execute(
            "INSERT INTO episodes(issue_id, issue_type, module, files, approach, outcome, rework_count, tokens, wall_sec,"
            " failure_reason, lesson, human_correction, confidence, evidence_count, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                issue,
                itype,
                module,
                json.dumps(files, ensure_ascii=False),
                str(patch.get("impl_log") or "")[:500],
                "pass" if outcome == "done" else "blocked",
                cp.rework,
                cp.usage.tokens,
                int(cp.usage.wall_s),
                blocked_reason or rework_reason,
                None,
                int(human_fix),
                float(confidence) if isinstance(confidence, (int, float)) else 0.5,
                1,
                ts,
            ),
        )
        _refresh_worker_stats(c, itype, module)
    approver = next(
        (
            str(e.human.who)
            for e in reversed(events)
            if e.event is EventType.GATE_RESOLVED
            and e.human
            and e.human.gate == "G1"
            and e.human.choice == "approve"
        ),
        None,
    )
    line = standup_line(cp, outcome, itype, module, blocked_reason, now, decision=decision, approver=approver)
    append_standup(layout, line)
    return RecordResult(issue, seq, outcome, roles, line)


def backfill_outcomes(*, db: Database, layout: HomeLayout, store: Any, checkpoints: Any) -> int:
    """Ghi số liệu cho issue đã kết thúc trước khi có tính năng này. Phiên bản JD ghi 0 (không rõ). Trả số issue thêm.

    Dùng chung cho `ddt perf --backfill` và lệnh backfill từ web (worker áp).
    """
    from digital_development_team.eventlog.reader import read_events

    done = [checkpoints.load(r.issue_id) for r in checkpoints.board() if r.status.terminal]
    added = 0
    for cp in sorted((c for c in done if c is not None), key=lambda c: c.saved_at):
        if already_recorded(db, cp.issue_id):
            continue

        def read_slot(slot: str, _issue: str = cp.issue_id) -> dict[str, Any]:
            try:
                return dict(store.read(_issue, slot).content)
            except DDTError:
                return {}

        record_issue_outcome(
            db=db,
            layout=layout,
            checkpoint=cp,
            events=read_events(layout, cp.issue_id),
            read_slot=read_slot,
            agent_versions={},
            unknown_version=True,
            now=cp.saved_at,
        )
        added += 1
    return added


def forget_outcome(db: Database, issue: str) -> bool:
    """Issue BLOCKED được mở lại: kết quả đã ghi không còn là kết quả cuối. Xoá role_outcomes (lần kết thúc sau ghi
    lại), episode cũ thành `superseded` (giữ lesson, không còn là bằng chứng pass/fail), bỏ job Learner của lần cũ,
    tính lại worker_stats. Event + blocked_report vẫn giữ lịch sử."""
    row = db.one("SELECT issue_type, module FROM role_outcomes WHERE issue_id=? LIMIT 1", (issue,))
    with db.transaction() as c:
        c.execute("DELETE FROM role_outcomes WHERE issue_id=?", (issue,))
        c.execute("UPDATE episodes SET outcome='superseded' WHERE issue_id=?", (issue,))
        c.execute("DELETE FROM learn_jobs WHERE issue_id=?", (issue,))
        if row is not None:
            _refresh_worker_stats(c, row["issue_type"], row["module"])
    return row is not None


def mark_bad_merge(db: Database, issue: str, *, by: str, note: str, at: datetime, auto_merged: bool) -> None:
    """Người xác nhận issue đã merge là sai. Những ai đã cho qua (Verifier, Security, Leader) bị tính fail cho issue
    này, episode ghi human_correction. auto_merged=1 → đếm vào "merge tự động sai" ở màn Đánh giá."""
    reason = f"người đánh dấu merge sai: {note[:120]}"
    with db.transaction() as c:
        c.execute(
            "INSERT OR REPLACE INTO merge_feedback(issue_id, by_actor, note, auto_merged, at) VALUES(?,?,?,?,?)",
            (issue, by, note[:500], int(auto_merged), at.isoformat()),
        )
        c.execute(
            "UPDATE role_outcomes SET passed=0, reason=? WHERE issue_id=? AND role IN ('verifier','security','leader')",
            (reason, issue),
        )
        c.execute("UPDATE episodes SET human_correction=1, outcome='bad_merge' WHERE issue_id=?", (issue,))
        row = c.execute(
            "SELECT issue_type, module FROM role_outcomes WHERE issue_id=? LIMIT 1", (issue,)
        ).fetchone()
        if row is not None:
            _refresh_worker_stats(c, row["issue_type"], row["module"])


def _refresh_worker_stats(c: Any, itype: str, module: str) -> None:
    """worker_stats = tổng hợp role_outcomes theo (role, issue_type, module). Tính lại phần bị ảnh hưởng."""
    c.execute("DELETE FROM worker_stats WHERE issue_type=? AND module=?", (itype, module))
    c.execute(
        "INSERT INTO worker_stats(role, issue_type, module, attempts, passes, tokens_sum)"
        " SELECT role, issue_type, module, SUM(passed IS NOT NULL), COALESCE(SUM(passed), 0), SUM(tokens)"
        " FROM role_outcomes WHERE issue_type=? AND module=? AND role != 'runner' GROUP BY role, issue_type, module",
        (itype, module),
    )


def standup_line(
    cp: Checkpoint,
    outcome: str,
    itype: str,
    module: str,
    blocked_reason: str | None,
    now: datetime,
    *,
    decision: Mapping[str, Any] | None = None,
    approver: str | None = None,
) -> str:
    """Một dòng mỗi issue. Ưu tiên standup_line Leader viết; không có thì harness tự dựng."""
    dec = dict(decision) if decision is not None else _facts(cp, "decision")
    tok = f"{round(cp.usage.tokens / 1000)}k tok"
    wall = cp.usage.wall_s
    took = "" if not wall else f"{max(1, round(wall))}s" if wall < 60 else f"{round(wall / 60)}m"
    split = _facts(cp, "split")
    if outcome == "done" and split.get("children"):
        return f"{now.strftime('%H:%M')} {cp.issue_id} {itype}/{module} — tách thành {', '.join(split['children'])}"
    if outcome == "done":
        if cp.facts.get("gate_rule_only"):
            how = "tự merge theo rule"
        elif approver:
            how = f"{approver.removeprefix('human:')} duyệt"
        else:
            how = "Leader merge"
        body = f"merged {cp.tier.value}, {how}, {cp.rework} rework, {tok}{', ' + took if took else ''}"
    else:
        body = f"bị chặn: {(blocked_reason or '?')[:80]}, {cp.rework} rework, {tok}"
    line = str(dec.get("standup_line") or "") if outcome == "done" and dec.get("standup_line") else ""
    return f"{now.strftime('%H:%M')} {cp.issue_id} {itype}/{module} — {line or body}"


def append_standup(layout: HomeLayout, line: str) -> None:
    p = layout.state_dir / "standup.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding=ENCODING, newline="\n") as f:
        f.write(line + "\n")


def read_standup(layout: HomeLayout, *, limit: int = 50) -> list[str]:
    p = layout.state_dir / "standup.md"
    if not p.is_file():
        return []
    lines = [ln for ln in p.read_text(encoding=ENCODING).splitlines() if ln.strip()]
    return lines[-limit:]


def familiarity(db: Database, *, module: str, role: str = "dev") -> float | None:
    """Tỷ lệ pass của role trên module khi đã có ≥ 2 lần đo được; None nếu chưa đủ (confidence dùng 0.4)."""
    row = db.one(
        "SELECT SUM(passed IS NOT NULL) n, COALESCE(SUM(passed), 0) p FROM role_outcomes WHERE role=? AND module=?",
        (role, module),
    )
    if row is None or not row["n"] or int(row["n"]) < MIN_ATTEMPTS_FOR_FAMILIARITY:
        return None
    return round(int(row["p"]) / int(row["n"]), 3)


# ───── truy vấn cho màn Đánh giá / perf review ─────
def _rate(p: Any, n: Any) -> float | None:
    return round(int(p) / int(n), 3) if n else None


def outcome_summary(db: Database) -> dict[str, Any]:
    roles = [
        {
            "role": r["role"],
            "issues": int(r["issues"]),
            "measured": int(r["measured"] or 0),
            "passes": int(r["passes"] or 0),
            "pass_rate": _rate(r["passes"] or 0, r["measured"]),
            "tokens": int(r["tokens"] or 0),
            "tok_per_task": round(int(r["tokens"] or 0) / int(r["issues"])) if r["issues"] else None,
            "cost_usd": round(float(r["cost"] or 0), 4),
        }
        for r in db.all(
            "SELECT role, COUNT(*) issues, SUM(passed IS NOT NULL) measured, SUM(passed) passes, SUM(tokens) tokens,"
            " SUM(cost_usd) cost FROM role_outcomes WHERE role != 'runner' GROUP BY role ORDER BY role"
        )
    ]
    versions = [
        {
            "role": r["role"],
            "version": int(r["agent_version"]),
            "issues": int(r["issues"]),
            "measured": int(r["measured"] or 0),
            "pass_rate": _rate(r["passes"] or 0, r["measured"]),
            "tok_per_task": round(int(r["tokens"] or 0) / int(r["issues"])) if r["issues"] else None,
            "first_seq": int(r["first_seq"]),
            "last_seq": int(r["last_seq"]),
        }
        for r in db.all(
            "SELECT role, agent_version, COUNT(*) issues, SUM(passed IS NOT NULL) measured, SUM(passed) passes,"
            " SUM(tokens) tokens, MIN(seq_no) first_seq, MAX(seq_no) last_seq FROM role_outcomes WHERE role != 'runner'"
            " GROUP BY role, agent_version ORDER BY role, agent_version"
        )
    ]
    modules = [
        {
            "module": r["module"],
            "issues": int(r["issues"]),
            "measured": int(r["measured"] or 0),
            "pass_rate": _rate(r["passes"] or 0, r["measured"]),
            "hard": bool(
                r["measured"]
                and int(r["measured"]) >= MIN_ATTEMPTS_FOR_FAMILIARITY
                and int(r["passes"] or 0) / int(r["measured"]) < 0.5
            ),
        }
        for r in db.all(
            "SELECT module, COUNT(*) issues, SUM(passed IS NOT NULL) measured, SUM(passed) passes FROM role_outcomes"
            " WHERE role='dev' GROUP BY module ORDER BY module"
        )
    ]
    curve = [
        {
            "seq": int(r["seq_no"]),
            "issue_id": r["issue_id"],
            "outcome": r["outcome"],
            "tier": r["tier"],
            "pass1": None if r["dev_pass"] is None else bool(r["dev_pass"]),
            "tokens": int(r["tokens"] or 0),
        }
        for r in db.all(
            "SELECT seq_no, issue_id, outcome, tier, MAX(CASE WHEN role='dev' THEN passed END) dev_pass, SUM(tokens) tokens"
            " FROM role_outcomes GROUP BY seq_no, issue_id ORDER BY seq_no"
        )
    ]
    episodes = db.all(
        "SELECT issue_id, outcome, rework_count, tokens, confidence, human_correction, module, issue_type FROM episodes ORDER BY id"
    )
    calibration = [
        {
            "issue_id": e["issue_id"],
            "confidence": e["confidence"],
            "ok": e["outcome"] == "pass" and not e["human_correction"],
        }
        for e in episodes
    ]

    def seg(lo: int, hi: int) -> dict[str, Any]:
        part = [c for c in curve if lo <= c["seq"] <= hi]
        meas = [c for c in part if c["pass1"] is not None]
        eps = [e for i, e in enumerate(episodes, 1) if lo <= i <= hi]
        return {
            "range": f"{lo}-{hi}" if hi < OPEN_END else f"{lo}+",
            "issues": len(part),
            "pass1": round(sum(1 for c in meas if c["pass1"]) / len(meas), 3) if meas else None,
            "tok_per_issue": round(sum(c["tokens"] for c in part) / len(part)) if part else None,
            "rework_avg": round(sum(int(e["rework_count"]) for e in eps) / len(eps), 2) if eps else None,
        }

    return {
        "issues": len(curve),
        "roles": roles,
        "versions": versions,
        "modules": modules,
        "curve": curve,
        "segments": [seg(1, 5), seg(6, 15), seg(16, OPEN_END)],
        "calibration": calibration,
    }


__all__ = [
    "RecordResult",
    "RoleOutcome",
    "already_recorded",
    "append_standup",
    "backfill_outcomes",
    "familiarity",
    "forget_outcome",
    "judge_roles",
    "mark_bad_merge",
    "outcome_summary",
    "read_standup",
    "record_issue_outcome",
    "standup_line",
]

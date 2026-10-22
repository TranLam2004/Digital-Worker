"""Biên bản bàn giao một issue (handover) — dùng làm PR description và biên bản cho người.

Dựng từ checkpoint · spec · evidence · sec_report · decision · gate_requests. Đọc slot qua ACL human (không diff).
Dùng chung cho `ddt handover ISSUE` và web /api/reports/issue/{id}/handover. 0 LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from digital_development_team.blackboard.checkpoints import CheckpointStore
from digital_development_team.blackboard.store import FileStore
from digital_development_team.core.context import Actor
from digital_development_team.core.errors import DDTError
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout

HUMAN = Actor(role="human")


@dataclass(frozen=True, slots=True)
class Handover:
    issue_id: str
    markdown: str
    data: dict[str, Any]


def _slot(store: FileStore, issue: str, slot: str) -> dict[str, Any]:
    try:
        return dict(store.read_as(issue, slot, HUMAN).content)
    except DDTError:
        return {}


def _fmt_dur(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    h, m, r = s // 3600, (s % 3600) // 60, s % 60
    return (f"{h}h " if h else "") + (f"{m}m " if m else "") + f"{r}s"


def _gate_rows(db: Database, issue: str) -> list[dict[str, Any]]:
    rows = db.all("SELECT * FROM gate_requests WHERE issue_id=? ORDER BY opened_at, rowid", (issue,))
    out: list[dict[str, Any]] = []
    for r in rows:
        wait = None
        if r["resolved_at"]:
            wait = int(
                (
                    datetime.fromisoformat(r["resolved_at"]) - datetime.fromisoformat(r["opened_at"])
                ).total_seconds()
            )
        wit = db.all("SELECT actor, action FROM gate_witness WHERE ticket_id=?", (r["id"],))
        out.append(
            {
                "gate": r["gate"],
                "tier": r["tier"],
                "status": r["status"],
                "choice": r["choice"],
                "decided_by": r["decided_by"],
                "note": r["note"],
                "opened_at": r["opened_at"],
                "resolved_at": r["resolved_at"],
                "wait_sec": wait,
                "witnesses": [f"{w['actor']}:{w['action']}" for w in wit],
            }
        )
    return out


def _answer_lines(intake: dict[str, Any], answer: dict[str, Any], title: str) -> list[str]:
    out = [
        "",
        "## Câu hỏi",
        str(intake.get("question") or title),
        "",
        "## Trả lời",
        str(answer.get("answer") or "—"),
    ]
    out += ["", "## Nguồn"] + [f"- {r}" for r in answer.get("references") or []]
    follow = [f for f in answer.get("follow_up") or [] if isinstance(f, dict)]
    if follow:
        out += ["", "## Nên mở issue tiếp"]
        out += [
            f"- [{f.get('type', '?')}] {f.get('title', '')}" + (f": {f['reason']}" if f.get("reason") else "")
            for f in follow
        ]
    conf = answer.get("confidence")
    out += [
        "",
        f"**Confidence của câu trả lời:** {float(conf):.2f}" if isinstance(conf, (int, float)) else "",
    ]
    return out


def _human_answer_lines(answers: Any) -> list[str]:
    """Câu người đã trả lời khi agent hỏi (escalation): người duyệt sau cần biết hướng đi do ai chọn."""
    rows = [a for a in answers or [] if isinstance(a, dict)]
    if not rows:
        return []
    out = ["", "## Người đã trả lời câu hỏi của agent"]
    for a in rows:
        who = str(a.get("by") or "?").removeprefix("human:")
        action = f": {a['chosen_action']}" if a.get("chosen_action") else ""
        out.append(
            f"- {a.get('stage', '?')} · {a.get('question', '')} → {who} chọn {a.get('choice')}{action}"
        )
        if a.get("note"):
            out.append(f"  - ghi chú: {a['note']}")
    return out


def build_handover(
    layout: HomeLayout, db: Database, store: FileStore, issue: str, *, now: datetime
) -> Handover:
    cp = CheckpointStore(layout, db).load(issue)
    if cp is None:
        raise DDTError(f"{issue}: chưa có checkpoint", hint="issue chưa chạy")
    spec = _slot(store, issue, "spec.yaml")
    ev = _slot(store, issue, "evidence.json")
    sec = _slot(store, issue, "sec_report.json")
    dec = _slot(store, issue, "decision.json")
    intake = _slot(store, issue, "intake.yaml")
    patch = _slot(store, issue, "patch/")
    gates = _gate_rows(db, issue)
    item: dict[str, Any] = cp.facts["item"] if isinstance(cp.facts.get("item"), dict) else {}
    sfh: dict[str, Any] = dec["summary_for_human"] if isinstance(dec.get("summary_for_human"), dict) else {}
    title = item.get("title") or sfh.get("title") or issue
    criteria = ev.get("criteria") or spec.get("acceptance_criteria") or []
    bumped = cp.facts.get("tier_bumped_because") or []
    conf = cp.facts.get("confidence")
    g1 = next((g for g in reversed(gates) if g["gate"] == "G1"), None)

    lines: list[str] = [f"# {issue} — {title}", ""]
    lines.append(
        f"**Loại:** {intake.get('type', '—')} · **Tier:** {cp.tier.value}"
        + (f" (bumped: {', '.join(map(str, bumped))})" if bumped else "")
        + f" · **Trạng thái:** {cp.status.value} @ {cp.stage.value}"
        + (f" · **Confidence:** {float(conf):.2f}" if isinstance(conf, (int, float)) else "")
    )
    answer = _slot(store, issue, "answer.yaml")
    if answer:  # issue loại câu hỏi: không có spec, patch, evidence
        lines += _answer_lines(intake, answer, title)
    else:
        lines += [
            "",
            "## Mục tiêu (spec)",
            str(spec.get("goal") or "—"),
            "",
            "## Acceptance criteria & bằng chứng",
        ]
        for c in criteria:
            if not isinstance(c, dict):
                continue
            mark = "x" if c.get("result") == "pass" else " "
            extra = f" — {c.get('result')}" if c.get("result") else ""
            evid = c.get("evidence") or c.get("evidence_ref")
            lines.append(
                f"- [{mark}] **{c.get('id', '?')}** {c.get('text', '')}{extra}"
                + (f" ({str(evid)[:120]})" if evid else "")
            )
        if not criteria:
            lines.append("- (chưa có)")
        reg = ev.get("regression") if isinstance(ev.get("regression"), dict) else {}
        lines += [
            "",
            f"**Verifier:** {ev.get('verdict', '—')}"
            + (f" · regression {reg.get('result')}" if reg else "")
            + f" · **Security:** {sec.get('verdict', 'n/a')}"
            + (f" ({sec.get('mode')})" if sec.get("mode") else ""),
            "",
            "## Thay đổi",
            f"- Files: {sfh.get('files_changed', len(patch.get('files_touched') or []))} · +{sfh.get('lines_added', '?')} -{sfh.get('lines_removed', '?')}",
            f"- Files touched: {', '.join(map(str, patch.get('files_touched') or [])) or '—'}",
            f"- Allowed files: {', '.join(map(str, spec.get('allowed_files') or [])) or '—'}",
            f"- Test: `{spec.get('test_command', '—')}`",
            f"- Rollback: {spec.get('rollback_plan', '—')}",
            f"- Commit: {patch.get('head_commit') or (cp.facts.get('merge') or {}).get('sha') or '—'} trên base {cp.base_commit or patch.get('base_commit') or '—'}",
            f"- Rework: {cp.rework} · Re-localize: {cp.reloc} · Token: {cp.usage.tokens} · Chi phí: ${cp.usage.cost_usd:.2f} · Thời gian máy: {_fmt_dur(cp.usage.wall_s)}",
            "",
            "## Rủi ro còn lại",
            str(sfh.get("top_risk") or dec.get("rationale") or "—"),
        ]
        findings = sec.get("findings") if isinstance(sec.get("findings"), list) else []
        if findings:
            lines += ["", "### Security findings"]
            for f in findings:
                if isinstance(f, dict):
                    lines.append(
                        f"- [{f.get('severity', '?')}] {f.get('title') or f.get('text') or f.get('id')}"
                        + (f" ({f.get('file')})" if f.get("file") else "")
                    )
        if isinstance(sec.get("incident_5c"), dict):
            lines += ["", "### 5C incident"]
            for k, v in sec["incident_5c"].items():
                lines.append(
                    f"- **{k}**: {v if not isinstance(v, dict) else ', '.join(f'{a}={b}' for a, b in v.items())}"
                )
    lines += _human_answer_lines(cp.facts.get("human_answers"))
    lines += ["", "## Phê duyệt"]
    if g1:
        lines.append(
            f"- G1 **{g1['choice'] or g1['status']}** by **{g1['decided_by'] or 'system'}** lúc {g1['resolved_at'] or '—'}"
            + (f" (chờ {_fmt_dur(g1['wait_sec'])})" if g1["wait_sec"] is not None else "")
            + (f" — {g1['note']}" if g1["note"] else "")
            + (f" · witness {', '.join(g1['witnesses'])}" if g1["witnesses"] else "")
        )
    else:
        lines.append("- chưa qua G1" + (" (T0/T1 rule-only)" if cp.facts.get("gate_rule_only") else ""))
    for g in gates:
        if g["gate"] != "G1":
            lines.append(
                f"- {g['gate']} {g['choice'] or g['status']} by {g['decided_by'] or '—'}"
                + (f" — {g['note']}" if g["note"] else "")
            )
    if dec.get("standup_line"):
        lines += ["", f"> {dec['standup_line']}"]
    lines += [
        "",
        f"_Sinh bởi DDT lúc {now.isoformat(timespec='seconds')} · nguồn: blackboard + gate_requests_",
        "",
    ]
    data = {
        "issue_id": issue,
        "title": title,
        "type": intake.get("type"),
        "tier": cp.tier.value,
        "status": cp.status.value,
        "stage": cp.stage.value,
        "confidence": conf,
        "criteria": criteria,
        "verifier": ev.get("verdict"),
        "security": sec.get("verdict"),
        "files_touched": patch.get("files_touched"),
        "gates": gates,
        "usage": cp.usage.model_dump(),
    }
    return Handover(issue, "\n".join(lines), data)

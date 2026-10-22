"""Báo cáo sự cố bảo mật cho một issue (reports/security_<issue>.md), dựng từ sec_report.json + events, 0 LLM.

Gồm: advisory 5 dòng (chuyện gì, ảnh hưởng, ở đâu, đã sửa thế nào, trạng thái) · 5C (Contain, Confirm, Correct,
Check, Communicate) · finding theo mức · việc cần làm ngay (xoay secret, báo người dùng) · dòng thời gian từ event ·
ai duyệt. Dùng chung cho `ddt security-report ISSUE` và màn Báo cáo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from digital_development_team.blackboard.checkpoints import CheckpointStore
from digital_development_team.blackboard.store import FileStore
from digital_development_team.core.enums import EventType
from digital_development_team.core.errors import DDTError, StoreError
from digital_development_team.eventlog.reader import read_events
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
C_LABEL = {
    "contain": "Contain (khoanh vùng)",
    "confirm": "Confirm (xác nhận)",
    "correct": "Correct (sửa tận gốc)",
    "check": "Check (kiểm lại)",
    "communicate": "Communicate (thông báo)",
}


@dataclass(frozen=True, slots=True)
class SecurityReport:
    issue_id: str
    markdown: str
    severity: str | None
    incident: bool


def _slot(store: FileStore, issue: str, slot: str) -> dict[str, Any]:
    try:
        return dict(store.read(issue, slot).content)
    except StoreError:
        return {}


def _yes(v: Any) -> str:
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "—"
    return "có" if v is True else "không" if v is False else "—"


def build_security_report(
    layout: HomeLayout, db: Database, store: FileStore, issue: str, *, now: datetime
) -> SecurityReport:
    cp = CheckpointStore(layout, db).load(issue)
    if cp is None:
        raise DDTError(f"{issue}: chưa có checkpoint", hint="issue chưa chạy")
    sec = _slot(store, issue, "sec_report.json")
    if not sec:
        raise StoreError(f"{issue}: chưa có sec_report.json", hint="Security chưa review issue này")
    intake = _slot(store, issue, "intake.yaml")
    spec = _slot(store, issue, "spec.yaml")
    patch = _slot(store, issue, "patch/")
    item: dict[str, Any] = cp.facts["item"] if isinstance(cp.facts.get("item"), dict) else {}
    findings = sorted(
        (f for f in sec.get("findings") or [] if isinstance(f, dict)),
        key=lambda f: (
            SEVERITY_ORDER.index(f.get("severity", "info")) if f.get("severity") in SEVERITY_ORDER else 9
        ),
    )
    top = findings[0].get("severity") if findings else None
    inc = sec.get("incident_5c") if isinstance(sec.get("incident_5c"), dict) else None
    title = item.get("title") or issue
    comm = (inc or {}).get("communicate") or {}

    lines = [f"# Báo cáo bảo mật {issue}: {title}", ""]
    lines += [
        f"**Chế độ review:** {sec.get('mode', '—')} · **Kết luận:** {sec.get('verdict', '—')} · "
        f"**Mức cao nhất:** {top or 'không có finding'} · **Tier:** {cp.tier.value} · **Trạng thái:** {cp.status.value}",
        "",
        "## Advisory",
        f"1. **Chuyện gì:** {(findings[0].get('description') if findings else None) or intake.get('rationale') or '—'}",
        f"2. **Ảnh hưởng:** {top or '—'} ({(findings[0].get('cwe_or_category') if findings else None) or 'chưa phân loại'})",
        f"3. **Ở đâu:** {', '.join(sorted({str(f.get('location')) for f in findings if f.get('location')})) or ', '.join(map(str, patch.get('files_touched') or [])) or '—'}",
        f"4. **Đã sửa thế nào:** {spec.get('goal') or '—'} (commit {patch.get('head_commit') or '—'})",
        f"5. **Trạng thái:** issue {cp.status.value}"
        + (
            f", PoC không còn tái hiện: {_yes((inc or {}).get('check', {}).get('poc_no_longer_reproduces'))}"
            if inc
            else ""
        ),
        "",
    ]
    if inc:
        lines += ["## 5C", ""]
        for key, label in C_LABEL.items():
            block = inc.get(key) if isinstance(inc.get(key), dict) else {}
            detail = (
                ", ".join(f"{k}: {_yes(v) if isinstance(v, (bool, list)) else v}" for k, v in block.items())
                or "—"
            )
            lines.append(f"- **{label}:** {detail}")
        lines.append("")
    lines += ["## Finding", ""]
    if findings:
        lines += ["| Mức | Loại | Ở đâu | Mô tả | Cách sửa |", "|---|---|---|---|---|"]
        for f in findings:
            desc = str(f.get("description", "")).replace("|", "/")
            fix = str(f.get("remediation", "—")).replace("|", "/")
            lines.append(
                f"| {f.get('severity')} | {f.get('cwe_or_category', '—')} | {f.get('location', '—')} | {desc} | {fix} |"
            )
    else:
        lines.append("- không có")
    actions = []
    if comm.get("secret_rotation_needed"):
        actions.append("Xoay secret liên quan ngay (token, key đã lộ hoặc nghi lộ).")
    if comm.get("user_notification_needed"):
        actions.append("Báo người dùng bị ảnh hưởng theo quy trình thông báo sự cố.")
    if inc and (inc.get("contain") or {}).get("status") == "proposed":
        actions.append(
            f"Áp biện pháp khoanh vùng đang đề xuất: {(inc.get('contain') or {}).get('measure', '—')}."
        )
    if inc and (inc.get("confirm") or {}).get("exploited_evidence") in ("suspected", "confirmed"):
        actions.append("Điều tra dấu vết khai thác trong log, vì có dấu hiệu đã bị khai thác.")
    lines += ["", "## Việc cần làm ngay", ""] + ([f"- {a}" for a in actions] or ["- không có"])
    lines += ["", "## Dòng thời gian", ""]
    for e in read_events(layout, issue):
        if e.event in (
            EventType.STAGE_ENTER,
            EventType.GATE_RESOLVED,
            EventType.MERGED,
            EventType.BLOCKED,
            EventType.ISSUE_DONE,
        ):
            what = e.event.value + (f" {e.stage.value}" if e.stage else "")
            if e.human:
                what += f" · {e.human.gate} {e.human.choice} bởi {e.human.who}"
            if e.data.get("sha"):
                what += f" · commit {e.data['sha']}"
            lines.append(f"- {e.ts.isoformat(timespec='seconds')} {what}")
    if sec.get("escalate_reason"):
        lines += ["", f"> Security đề nghị người xem: {sec['escalate_reason']}"]
    lines += [
        "",
        f"_Sinh bởi DDT lúc {now.isoformat(timespec='seconds')} từ sec_report.json và events, 0 LLM._",
    ]
    return SecurityReport(issue, "\n".join(lines) + "\n", top, inc is not None)


__all__ = ["SecurityReport", "build_security_report"]

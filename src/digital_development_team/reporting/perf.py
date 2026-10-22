"""Performance review mỗi N issue (50-knowledge/05-profiles): reports/perf_<N>.md, 0 LLM.

Bảng theo role (pass gần đây so với nhóm đầu, token mỗi task), so sánh phiên bản JD (A/B prompt miễn phí khi
sửa JD qua override), module khó cho Dev. Runner gọi `maybe_write_perf` sau mỗi issue kết thúc.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from digital_development_team.memory.outcomes import outcome_summary
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import write_text_atomic

PERF_EVERY = 10


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{round(x * 100)}%"


def _window(db: Database, lo: int, hi: int) -> dict[str, dict[str, Any]]:
    rows = db.all(
        "SELECT role, COUNT(*) n, SUM(passed IS NOT NULL) m, SUM(passed) p, SUM(tokens) t FROM role_outcomes"
        " WHERE seq_no BETWEEN ? AND ? AND role != 'runner' GROUP BY role",
        (lo, hi),
    )
    return {
        r["role"]: {
            "n": int(r["n"]),
            "rate": (int(r["p"] or 0) / int(r["m"])) if r["m"] else None,
            "tok": (int(r["t"] or 0) / int(r["n"])) if r["n"] else None,
        }
        for r in rows
    }


def build_perf_review(
    db: Database, *, upto: int, every: int = PERF_EVERY, now: datetime | None = None
) -> str:
    first = _window(db, 1, min(every, upto))
    recent = _window(db, max(1, upto - every + 1), upto)
    compare = upto > every  # chưa đủ hai cửa sổ thì không có gì để so
    summ = outcome_summary(db)
    lines = [f"# Performance review sau {upto} issue", ""]
    if now is not None:
        lines += [f"_Sinh lúc {now.isoformat(timespec='seconds')} từ role_outcomes, 0 LLM._", ""]
    lines += [
        f"## Theo role ({every} issue gần nhất so với {every} issue đầu)"
        if compare
        else f"## Theo role ({upto} issue)",
        "",
        "| Role | Pass | Δ | Tok/task | Δ | Số issue |",
        "|---|---|---|---|---|---|",
    ]
    for role in sorted(set(first) | set(recent)):
        a, b = first.get(role, {}), recent.get(role, {})
        dr = None if not compare or a.get("rate") is None or b.get("rate") is None else b["rate"] - a["rate"]
        dt = None if not compare or not a.get("tok") or not b.get("tok") else (b["tok"] - a["tok"]) / a["tok"]
        lines.append(
            f"| {role} | {_pct(b.get('rate'))} | {'—' if dr is None else f'{dr * 100:+.0f} điểm'} | "
            f"{'—' if b.get('tok') is None else round(b['tok'])} | {'—' if dt is None else f'{dt * 100:+.0f}%'} | {b.get('n', 0)} |"
        )
    multi = [v for v in summ["versions"] if sum(1 for x in summ["versions"] if x["role"] == v["role"]) > 1]
    if multi:
        lines += [
            "",
            "## So sánh phiên bản JD",
            "",
            "| Role | Phiên bản | Issue | Pass | Tok/task | Áp dụng từ issue |",
            "|---|---|---|---|---|---|",
        ]
        for v in multi:
            lines.append(
                f"| {v['role']} | v{v['version']} | {v['issues']} | {_pct(v['pass_rate'])} | {v['tok_per_task'] or '—'} | #{v['first_seq']} |"
            )
    hard = [m for m in summ["modules"] if m["hard"]]
    lines += ["", "## Module khó cho Dev", ""]
    lines += [f"- **{m['module']}**: pass {_pct(m['pass_rate'])} trên {m['measured']} lần" for m in hard] or [
        "- không có"
    ]
    seg = summ["segments"]
    lines += [
        "",
        "## Học hỏi",
        "",
        "| Nhóm issue | Số issue | pass@1 | Token/issue | Rework TB |",
        "|---|---|---|---|---|",
    ]
    for s in seg:
        lines.append(
            f"| {s['range']} | {s['issues']} | {_pct(s['pass1'])} | {s['tok_per_issue'] or '—'} | {s['rework_avg'] if s['rework_avg'] is not None else '—'} |"
        )
    return "\n".join(lines) + "\n"


def maybe_write_perf(
    layout: HomeLayout, db: Database, seq_no: int, *, now: datetime, every: int = PERF_EVERY
) -> Path | None:
    """Ghi reports/perf_<seq>.md khi seq_no chia hết cho `every`."""
    if seq_no <= 0 or seq_no % every:
        return None
    path = layout.reports_dir / f"perf_{seq_no:03d}.md"
    write_text_atomic(path, build_perf_review(db, upto=seq_no, every=every, now=now), fsync=False)
    return path

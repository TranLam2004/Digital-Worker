"""Retro mỗi N issue (70-human-control/07-rituals): 3 điều tốt · 3 điều tệ · 3 hành động, 0 LLM.

Nguồn: role_outcomes (pass theo role, module), episodes (lý do bị chặn), gate_requests (lý do người từ chối),
canary_results, merge_feedback. So cửa sổ N issue gần nhất với N issue trước đó. Hành động suy ra từ điều tệ, có
chỗ để làm (màn và tab). reports/retro_NNN.md + reports/retro_NNN.actions.json (P3 tick đã làm trên dashboard).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING, write_text_atomic

RETRO_EVERY = 10


def _window_issues(db: Database, lo: int, hi: int) -> list[str]:
    rows = db.all("SELECT DISTINCT issue_id FROM role_outcomes WHERE seq_no BETWEEN ? AND ?", (lo, hi))
    return [str(r["issue_id"]) for r in rows]


def _role_rates(db: Database, lo: int, hi: int) -> dict[str, tuple[float, int]]:
    rows = db.all(
        "SELECT role, SUM(passed IS NOT NULL) m, COALESCE(SUM(passed), 0) p FROM role_outcomes"
        " WHERE seq_no BETWEEN ? AND ? AND role != 'runner' GROUP BY role",
        (lo, hi),
    )
    return {str(r["role"]): (int(r["p"]) / int(r["m"]), int(r["m"])) for r in rows if r["m"]}


def _tokens_per_issue(db: Database, lo: int, hi: int) -> float | None:
    row = db.one(
        "SELECT SUM(tokens) t, COUNT(DISTINCT issue_id) n FROM role_outcomes WHERE seq_no BETWEEN ? AND ?",
        (lo, hi),
    )
    return (int(row["t"] or 0) / int(row["n"])) if row and row["n"] else None


def build_retro(db: Database, *, upto: int, every: int = RETRO_EVERY) -> dict[str, Any]:
    lo, hi = max(1, upto - every + 1), upto
    plo, phi = max(1, lo - every), lo - 1
    ids = _window_issues(db, lo, hi)
    now_rates, prev_rates = _role_rates(db, lo, hi), (_role_rates(db, plo, phi) if phi >= plo else {})
    good: list[tuple[float, str]] = []
    bad: list[tuple[float, str, str]] = []  # (điểm, mô tả, hành động)
    for role, (rate, n) in now_rates.items():
        before = prev_rates.get(role)
        if before and rate - before[0] >= 0.15:
            good.append((rate - before[0], f"{role} tiến bộ: pass {before[0]:.0%} → {rate:.0%} ({n} lần đo)"))
        elif rate >= 0.9 and n >= 3:
            good.append((rate - 0.9, f"{role} ổn định: pass {rate:.0%} trên {n} lần đo"))
        if rate < 0.6 and n >= 3:
            bad.append(
                (
                    0.6 - rate,
                    f"{role} pass thấp: {rate:.0%} trên {n} lần đo",
                    f"Xem lại JD của {role}: so phiên bản ở màn Đánh giá, sửa ở Team / {role} / JD",
                )
            )
    t_now, t_prev = _tokens_per_issue(db, lo, hi), (_tokens_per_issue(db, plo, phi) if phi >= plo else None)
    if t_now and t_prev and t_now <= t_prev * 0.9:
        good.append(
            (
                (t_prev - t_now) / t_prev,
                f"Token mỗi issue giảm {1 - t_now / t_prev:.0%} ({round(t_prev)} → {round(t_now)})",
            )
        )
    elif t_now and t_prev and t_now >= t_prev * 1.2:
        bad.append(
            (
                (t_now - t_prev) / t_prev,
                f"Token mỗi issue tăng {t_now / t_prev - 1:.0%}",
                "Bật economy hoặc hạ model cho role tốn nhất ở Cài đặt; xem Chi phí theo model",
            )
        )
    win = (lo, hi)  # issue trong cửa sổ = có role_outcomes với seq_no trong khoảng
    hard = db.all(
        "SELECT module, COUNT(*) n, COALESCE(SUM(passed), 0) p FROM role_outcomes WHERE role='dev' AND passed IS NOT NULL"
        " AND issue_id IN (SELECT issue_id FROM role_outcomes WHERE seq_no BETWEEN ? AND ?) GROUP BY module",
        win,
    )
    for r in hard:
        n, p = int(r["n"]), int(r["p"])
        if n >= 2 and p / n < 0.5:
            bad.append(
                (
                    1 - p / n,
                    f"Module {r['module']} khó cho Dev: pass {p}/{n}",
                    f"Thêm fact KB cho module {r['module']} (Team / KB) và cân nhắc checklist riêng cho module này",
                )
            )
        elif n >= 3 and p == n:
            good.append((0.2, f"Module {r['module']} Dev làm đúng ngay cả {n} lần"))
    rejects = db.all(
        "SELECT note FROM gate_requests WHERE status='resolved' AND choice LIKE 'reject%' AND note IS NOT NULL"
        " AND issue_id IN (SELECT issue_id FROM role_outcomes WHERE seq_no BETWEEN ? AND ?)",
        win,
    )
    reasons: dict[str, int] = {}
    for r in rejects:
        key = str(r["note"]).split(":")[0].strip()[:60]
        reasons[key] = reasons.get(key, 0) + 1
    for reason, n in sorted(reasons.items(), key=lambda x: -x[1])[:2]:
        low = reason.lower()
        if "test" in low:
            act = "Gán skill test-driven-development cho Dev (Team / Dev / Skill) và nhắc Analyst ghi AC verify_by test"
        elif "phạm vi" in low or "ngoài" in low or "scope" in low:
            act = "Nhắc Analyst siết allowed_files trong spec (Team / Analyst / JD)"
        elif "bảo mật" in low or "security" in low:
            act = "Gán checklist _base/security cho Dev và xem lại sec_report các issue bị từ chối"
        else:
            act = f'Đọc lại các issue bị từ chối vì "{reason}" ở màn Báo cáo / Biên bản quyết định gate'
        bad.append((0.3 + 0.1 * n, f"Người từ chối {n} lần vì: {reason}", act))
    blocked = db.all(
        "SELECT failure_reason FROM episodes WHERE outcome='blocked' AND issue_id IN (SELECT issue_id FROM role_outcomes WHERE seq_no BETWEEN ? AND ?)",
        win,
    )
    if blocked:
        reason = str(blocked[0]["failure_reason"] or "?")[:80]
        act = (
            "Tăng budget stage bị hết hoặc tách issue nhỏ hơn (Cài đặt / File config / budgets)"
            if "BUDGET" in reason or "thời gian" in reason
            else "Mở các issue bị chặn ở màn Issues, chạy lại hoặc sửa backlog cho rõ hơn"
        )
        bad.append((0.25 * len(blocked), f"{len(blocked)} issue bị chặn, ví dụ: {reason}", act))
    elif ids:
        good.append((0.15, f"Không issue nào bị chặn trong {len(ids)} issue"))
    missed = db.all(
        "SELECT mutation FROM canary_results WHERE caught=0 AND issue_id IN (SELECT issue_id FROM role_outcomes WHERE seq_no BETWEEN ? AND ?)",
        win,
    )
    if missed:
        bad.append(
            (
                0.5,
                f"Verifier bỏ lọt {len(missed)} canary ({', '.join(sorted({str(m['mutation']) for m in missed}))})",
                "Xem lại skill adversarial-review của Verifier (Team / Verifier / Skill)",
            )
        )
    bad_merges = db.all(
        "SELECT issue_id, note FROM merge_feedback WHERE issue_id IN (SELECT issue_id FROM role_outcomes WHERE seq_no BETWEEN ? AND ?)",
        win,
    )
    if bad_merges:
        bad.append(
            (
                0.6,
                f"{len(bad_merges)} issue merge sai, ví dụ {bad_merges[0]['issue_id']}: {str(bad_merges[0]['note'])[:60]}",
                "Xem lại checklist của Verifier và thêm AC kiểm hồi quy cho phần đã hỏng",
            )
        )
    good_items = [g for _, g in sorted(good, key=lambda x: -x[0])][:3]
    bad_sorted = sorted(bad, key=lambda x: -x[0])[:3]
    actions = [{"id": f"A{i + 1}", "text": a, "done": False} for i, (_, _, a) in enumerate(bad_sorted)]
    if len(actions) < 3:
        actions.append(
            {
                "id": f"A{len(actions) + 1}",
                "text": "Giữ nguyên cấu hình, theo dõi thêm 10 issue",
                "done": False,
            }
        )
    return {
        "upto": upto,
        "range": f"{lo}-{hi}",
        "issues": len(ids),
        "good": good_items or ["Chưa đủ số liệu để chỉ ra điểm tốt"],
        "bad": [b for _, b, _ in bad_sorted] or ["Không thấy vấn đề nổi bật"],
        "actions": actions[:3],
    }


def render_retro(r: dict[str, Any], *, now: datetime | None = None) -> str:
    lines = [f"# Retro sau {r['upto']} issue (nhóm {r['range']}, {r['issues']} issue)", ""]
    if now is not None:
        lines += [f"_Sinh lúc {now.isoformat(timespec='seconds')} từ số liệu học hỏi, 0 LLM._", ""]
    lines += ["## 3 điều tốt", ""] + [f"- {g}" for g in r["good"]]
    lines += ["", "## 3 điều tệ", ""] + [f"- {b}" for b in r["bad"]]
    lines += ["", "## 3 hành động", ""] + [
        f"- [{'x' if a['done'] else ' '}] **{a['id']}** {a['text']}" for a in r["actions"]
    ]
    return "\n".join(lines) + "\n"


def actions_path(layout: HomeLayout, name: str) -> Path:
    return layout.reports_dir / f"{Path(name).stem}.actions.json"


def maybe_write_retro(
    layout: HomeLayout, db: Database, seq_no: int, *, now: datetime, every: int = RETRO_EVERY
) -> Path | None:
    if seq_no <= 0 or seq_no % every:
        return None
    r = build_retro(db, upto=seq_no, every=every)
    path = layout.reports_dir / f"retro_{seq_no:03d}.md"
    write_text_atomic(path, render_retro(r, now=now), fsync=False)
    write_text_atomic(
        actions_path(layout, path.name), json.dumps(r["actions"], ensure_ascii=False, indent=2), fsync=False
    )
    return path


def read_actions(layout: HomeLayout, name: str) -> list[dict[str, Any]]:
    p = actions_path(layout, name)
    return list(json.loads(p.read_text(encoding=ENCODING))) if p.is_file() else []


def set_action(
    layout: HomeLayout, name: str, action_id: str, *, done: bool, by: str, at: datetime
) -> list[dict[str, Any]]:
    """P3 tick hành động đã làm. Ghi lại cả file .md để bản đọc khớp ô tick."""
    items = read_actions(layout, name)
    for a in items:
        if a.get("id") == action_id:
            a["done"] = done
            a["by"] = by
            a["at"] = at.isoformat()
    write_text_atomic(
        actions_path(layout, name), json.dumps(items, ensure_ascii=False, indent=2), fsync=False
    )
    md = layout.reports_dir / name
    if md.is_file():
        text = md.read_text(encoding=ENCODING)
        for a in items:
            mark = "x" if a.get("done") else " "
            for old in ("[ ]", "[x]"):
                text = text.replace(f"- {old} **{a['id']}**", f"- [{mark}] **{a['id']}**")
        write_text_atomic(md, text, fsync=False)
    return items


__all__ = ["RETRO_EVERY", "build_retro", "maybe_write_retro", "read_actions", "render_retro", "set_action"]

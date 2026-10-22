"""Skill học được (50-knowledge/04-procedural): Learner đề xuất, harness quét, dùng thử, tự lên khi đủ bằng chứng.

Vòng đời mỗi bản (version):
  quarantine  quét không qua (memory/skill_scan.py) — không agent nào thấy; Learner sửa bằng bản mới
  candidate   quét qua — brief nhận tối đa `candidates_per_brief` skill loại này, có nhãn chưa kiểm chứng
  active      ≥ auto_promote_applied lần dùng đo được, tỉ lệ pass ≥ auto_promote_rate (hoặc người duyệt)
  retired     confidence < conf_delete_below, hoặc người gỡ
  rejected    người từ chối

Bản mới (đề xuất lại cùng tên hoặc patch) luôn bắt đầu lại từ quét → candidate; số lần dùng tính theo bản.
Nội dung: `<home>/memory/skills/<name>/v<N>.SKILL.md` (giữ mọi bản để xem diff). Trạng thái: bảng learned_skills.
Verifier/Security không bao giờ nhận skill học được (NO_MEMORY_ROLES).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from digital_development_team.config.models import MemoryCfg
from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.core.errors import StoreError
from digital_development_team.memory import NO_MEMORY_ROLES
from digital_development_team.memory.skill_scan import NAME_RE, ScanContext, scan_skill
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.text import estimate_tokens

STATUSES = ("quarantine", "candidate", "active", "retired", "rejected")
HUMAN_ACTIONS = {"approve": "active", "reject": "rejected", "retire": "retired"}
_PAST = {"approve": "approved", "reject": "rejected", "retire": "retired"}
Verdict = Literal["pass", "soft_fail", "fail"]


@dataclass(frozen=True)
class LearnedSkill:
    name: str
    version: int
    status: str
    description: str
    module: str
    issue_type: str
    used_by: tuple[str, ...]
    confidence: float
    applied: int
    passed: int
    problems: tuple[str, ...]
    source_issues: tuple[str, ...]
    created_by: str
    created_at: str
    updated_at: str

    @property
    def ref(self) -> str:
        return f"skill:{self.name}@{self.version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "description": self.description,
            "module": self.module,
            "issue_type": self.issue_type,
            "used_by": list(self.used_by),
            "confidence": round(self.confidence, 3),
            "applied": self.applied,
            "passed": self.passed,
            "problems": list(self.problems),
            "source_issues": list(self.source_issues),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def score(conf: float, applied: int, passed: int, verdict: Verdict, cfg: MemoryCfg) -> tuple[float, int, int]:
    """Một lần dùng đo được → (confidence, applied, passed) mới. Dùng chung cho skill và fact."""
    delta = {"pass": cfg.conf_on_pass, "soft_fail": cfg.conf_on_soft_fail, "fail": cfg.conf_on_fail}[verdict]
    return min(1.0, max(0.0, conf + delta)), applied + 1, passed + (verdict == "pass")


def next_status(status: str, conf: float, applied: int, passed: int, cfg: MemoryCfg) -> str:
    if status in ("candidate", "active") and conf < cfg.conf_delete_below:
        return "retired"
    if (
        status == "candidate"
        and applied >= cfg.auto_promote_applied
        and passed / applied >= cfg.auto_promote_rate
    ):
        return "active"
    return status


def _row(r: Any) -> LearnedSkill:
    return LearnedSkill(
        name=str(r["name"]),
        version=int(r["version"]),
        status=str(r["status"]),
        description=str(r["description"]),
        module=str(r["module"]),
        issue_type=str(r["issue_type"]),
        used_by=tuple(json.loads(r["used_by"] or "[]")),
        confidence=float(r["confidence"]),
        applied=int(r["applied"]),
        passed=int(r["passed"]),
        problems=tuple(json.loads(r["problems"] or "[]")),
        source_issues=tuple(json.loads(r["source_issues"] or "[]")),
        created_by=str(r["created_by"]),
        created_at=str(r["created_at"]),
        updated_at=str(r["updated_at"]),
    )


class LearnedSkills:
    def __init__(
        self, db: Database, layout: HomeLayout, cfg: MemoryCfg, *, clock: Clock | None = None
    ) -> None:
        self._db = db
        self._dir = layout.memory_dir / "skills"
        self._cfg = cfg
        self._clock = clock or SystemClock()

    # ───── đọc ─────
    def get(self, name: str) -> LearnedSkill | None:
        r = self._db.one("SELECT * FROM learned_skills WHERE name=?", (name,))
        return _row(r) if r is not None else None

    def list_skills(self, *, status: str | None = None) -> list[LearnedSkill]:
        if status:
            rows = self._db.all(
                "SELECT * FROM learned_skills WHERE status=? ORDER BY updated_at DESC", (status,)
            )
        else:
            rows = self._db.all("SELECT * FROM learned_skills ORDER BY updated_at DESC")
        return [_row(r) for r in rows]

    def path(self, name: str, version: int) -> Path:
        return self._dir / name / f"v{version}.SKILL.md"

    def text(self, name: str, version: int | None = None) -> str:
        s = self.get(name)
        if s is None:
            raise StoreError(f"không có skill học được {name!r}")
        p = self.path(name, version or s.version)
        if not p.is_file():
            raise StoreError(f"không có {name} v{version or s.version}")
        return p.read_text(encoding=ENCODING)

    def history(self, name: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._db.all(
                "SELECT version, event, by_actor, issue_id, note, at FROM skill_events WHERE name=? ORDER BY id",
                (name,),
            )
        ]

    def for_brief(self, role: str, *, module: str, issue_type: str) -> list[LearnedSkill]:
        """Active khớp (≤ skills_per_brief) rồi candidate khớp (≤ candidates_per_brief); cụ thể trước wildcard."""
        if role in NO_MEMORY_ROLES:
            return []
        rows = self._db.all(
            "SELECT * FROM learned_skills WHERE status IN ('active','candidate')"
            " AND module IN (?, '*') AND issue_type IN (?, '*') ORDER BY confidence DESC, updated_at DESC",
            (module or "*", issue_type or "*"),
        )
        match = [s for s in map(_row, rows) if role in s.used_by]
        match.sort(key=lambda s: (s.module == "*") + (s.issue_type == "*"))
        active = [s for s in match if s.status == "active"][: self._cfg.skills_per_brief]
        cand = [s for s in match if s.status == "candidate"][: self._cfg.candidates_per_brief]
        return active + cand

    # ───── ghi ─────
    def propose(self, text: str, *, by: str, issue: str | None, scan: ScanContext) -> LearnedSkill:
        """Skill mới hoặc bản mới của skill cùng tên. Quét qua → candidate; không qua → quarantine kèm lý do."""
        meta, _body, problems = scan_skill(text, scan)
        name = str(meta.get("name") or "")
        if not NAME_RE.match(name):
            raise StoreError(
                "tên skill phải dạng learned-<slug>", hint="; ".join(problems) or "name: learned-<slug>"
            )
        old = self.get(name)
        if old is not None and old.status == "rejected" and not by.startswith("human"):
            raise StoreError(f"{name} đã bị người từ chối", hint="đặt tên khác hoặc nhờ người mở lại")
        version = (old.version + 1) if old else 1
        now = self._clock.now().isoformat()
        p = self.path(name, version)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(p, text.replace("\r\n", "\n").rstrip() + "\n", fsync=False)
        status = "quarantine" if problems else "candidate"
        sources = list(old.source_issues if old else ())
        if issue and issue not in sources:
            sources.append(issue)
        used_by = [str(r) for r in (meta.get("used_by") or ["dev"]) if isinstance(r, str)]
        values = (
            version,
            status,
            hashlib.sha256(text.encode(ENCODING)).hexdigest(),
            str(meta.get("description") or "")[:300],
            str(meta.get("module") or "*"),
            str(meta.get("issue_type") or "*"),
            json.dumps(used_by),
            self._cfg.promote_initial_conf,
            json.dumps(problems, ensure_ascii=False),
            json.dumps(sources[-20:]),
            now,
        )
        with self._db.transaction() as c:
            if old is None:
                c.execute(
                    "INSERT INTO learned_skills(version, status, sha256, description, module, issue_type, used_by,"
                    " confidence, problems, source_issues, updated_at, name, created_by, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, name, by, now),
                )
            else:
                c.execute(
                    "UPDATE learned_skills SET version=?, status=?, sha256=?, description=?, module=?, issue_type=?,"
                    " used_by=?, confidence=?, problems=?, source_issues=?, updated_at=?, applied=0, passed=0"
                    " WHERE name=?",
                    (*values, name),
                )
            self._event(c, name, version, "proposed", by, issue, f"v{version}")
            self._event(c, name, version, status, "harness", issue, "; ".join(problems)[:500] or None)
        return self._must(name)

    def patch(
        self, name: str, find: str, replace: str, *, by: str, issue: str | None, scan: ScanContext
    ) -> LearnedSkill:
        """Sửa nhỏ bản hiện tại (find xuất hiện đúng 1 lần) → bản mới, quét lại từ đầu."""
        cur = self.text(name)
        if not find or cur.count(find) != 1:
            raise StoreError(
                "find phải xuất hiện đúng 1 lần trong skill", hint="memory.get(id) để xem nội dung hiện tại"
            )
        return self.propose(cur.replace(find, replace, 1), by=by, issue=issue, scan=scan)

    def set_status(self, name: str, action: str, *, by: str, note: str = "") -> LearnedSkill:
        """Người: approve · reject · retire."""
        s = self.get(name)
        if s is None:
            raise StoreError(f"không có skill học được {name!r}")
        target = HUMAN_ACTIONS.get(action)
        if target is None:
            raise StoreError(f"hành động lạ: {action!r}", hint="approve · reject · retire")
        if action == "approve" and s.status == "quarantine":
            raise StoreError(
                f"{name} v{s.version} chưa qua quét: {'; '.join(s.problems)[:200]}",
                hint="sửa nội dung (bản mới) rồi duyệt",
            )
        with self._db.transaction() as c:
            c.execute(
                "UPDATE learned_skills SET status=?, updated_at=? WHERE name=?",
                (target, self._clock.now().isoformat(), name),
            )
            self._event(c, name, s.version, _PAST[action], by, None, note or None)
        return self._must(name)

    def record_use(self, refs: Iterable[str], *, issue: str, verdict: Verdict) -> list[str]:
        """Issue đã dùng skill (ref `skill:<name>@<v>`) kết thúc → chấm điểm bản đó. Trả mô tả thay đổi trạng thái."""
        changes: list[str] = []
        now = self._clock.now().isoformat()
        for ref in refs:
            if not ref.startswith("skill:learned-") or "@" not in ref:
                continue
            name, _, ver = ref.removeprefix("skill:").partition("@")
            s = self.get(name)
            if s is None or str(s.version) != ver or s.status not in ("candidate", "active"):
                continue  # bản cũ / đã gỡ: không tính cho bản hiện tại
            conf, applied, passed = score(s.confidence, s.applied, s.passed, verdict, self._cfg)
            status = next_status(s.status, conf, applied, passed, self._cfg)
            with self._db.transaction() as c:
                c.execute(
                    "UPDATE learned_skills SET confidence=?, applied=?, passed=?, status=?, updated_at=? WHERE name=?",
                    (conf, applied, passed, status, now, name),
                )
                self._event(c, name, s.version, "used", "harness", issue, f"{verdict} → conf {conf:.2f}")
                if status != s.status:
                    ev = "promoted" if status == "active" else status
                    self._event(c, name, s.version, ev, "harness", issue, f"{passed}/{applied} pass")
                    changes.append(f"{name}: {s.status} → {status}")
        return changes

    def _must(self, name: str) -> LearnedSkill:
        got = self.get(name)
        if got is None:  # pragma: no cover — vừa ghi trong transaction
            raise StoreError(f"không đọc lại được {name}")
        return got

    def _event(
        self, c: Any, name: str, version: int, event: str, by: str, issue: str | None, note: str | None
    ) -> None:
        c.execute(
            "INSERT INTO skill_events(name, version, event, by_actor, issue_id, note, at) VALUES(?,?,?,?,?,?,?)",
            (name, version, event, by, issue, note, self._clock.now().isoformat()),
        )


def brief_text(s: LearnedSkill, body: str) -> tuple[str, int]:
    """Phần đưa vào brief: tiêu đề + nhãn + thân skill (bỏ frontmatter)."""
    label = (
        " (learned, team-proven)"
        if s.status == "active"
        else " (learned candidate — not yet proven; use only if it clearly fits, the spec wins)"
    )
    text = f"### SKILL: {s.name}{label}\n{body.strip()}"
    return text, estimate_tokens(text)


__all__ = [
    "HUMAN_ACTIONS",
    "STATUSES",
    "LearnedSkill",
    "LearnedSkills",
    "Verdict",
    "brief_text",
    "next_status",
    "score",
]

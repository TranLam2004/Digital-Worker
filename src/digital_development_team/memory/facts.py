"""Fact trong KB (`<home>/memory/facts.json`): điều đã biết về repo, người duyệt hoặc Learner đề xuất.

Vòng đời: candidate → active (vào brief) → retired (hạ, giữ lịch sử).
- Người thêm fact trên web là active ngay; nâng/hạ bằng nút trên màn Team › KB. Harness không tự hạ fact của người.
- Learner đề xuất (memory.promote_fact) cần ≥ promote_min_evidence issue pass làm bằng chứng → candidate. Candidate vào
  brief có nhãn chưa kiểm chứng; harness chấm theo kết quả issue đã dùng nó (cùng luật với skill học được) → tự lên
  active hoặc retired.
FactsMemory đưa fact đúng module vào khối RELEVANT EXPERIENCE của brief (trừ Verifier/Security: NO_MEMORY_ROLES).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from digital_development_team.config.models import MemoryCfg
from digital_development_team.core.context import Clock, SystemClock
from digital_development_team.core.errors import StoreError
from digital_development_team.memory.learned import Verdict, next_status, score
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util import jsonio
from digital_development_team.util.fsio import write_text_atomic
from digital_development_team.util.text import estimate_tokens

STATUSES = ("candidate", "active", "retired")
MAX_TEXT = 300


class FactStore:
    def __init__(self, layout: HomeLayout, *, clock: Clock | None = None) -> None:
        self._path = layout.memory_dir / "facts.json"
        self._clock = clock or SystemClock()

    def list_facts(self, *, status: str | None = None, module: str | None = None) -> list[dict[str, Any]]:
        facts = self._load()
        if status:
            facts = [f for f in facts if f.get("status") == status]
        if module:
            facts = [f for f in facts if f.get("module") in (module, "*", "")]
        return facts

    def get(self, fact_id: str) -> dict[str, Any] | None:
        return next((f for f in self._load() if f.get("id") == fact_id), None)

    def apply(self, payload: Mapping[str, Any], *, by: str) -> dict[str, Any]:
        """add · promote · demote · edit. Trả fact sau khi đổi."""
        action = str(payload.get("action") or "")
        facts = self._load()
        now = self._clock.now().isoformat()
        if action == "add":
            text = str(payload.get("text") or "").strip()
            if len(text) < 10:
                raise StoreError("fact quá ngắn", hint="viết một câu cụ thể, tối thiểu 10 ký tự")
            status = str(payload.get("status") or ("active" if by.startswith("human") else "candidate"))
            fact: dict[str, Any] = {
                "id": f"F{len(facts) + 1:04d}",
                "text": text[:MAX_TEXT],
                "module": str(payload.get("module") or "*").strip() or "*",
                "status": status if status in STATUSES else "candidate",
                "source": "human" if by.startswith("human") else "learner",
                "created_by": by,
                "created_at": now,
                "history": [{"at": now, "by": by, "action": "add"}],
            }
            facts.append(fact)
            self._save(facts)
            return fact
        found: dict[str, Any] | None = next((f for f in facts if f.get("id") == payload.get("id")), None)
        if found is None:
            raise StoreError(f"không có fact {payload.get('id')!r}")
        fact = found
        if action == "promote":
            fact["status"] = "active"
        elif action == "demote":
            fact["status"] = "retired"
        elif action == "edit":
            if payload.get("text"):
                fact["text"] = str(payload["text"]).strip()[:MAX_TEXT]
            if payload.get("module") is not None:
                fact["module"] = str(payload["module"]).strip() or "*"
        else:
            raise StoreError(f"hành động lạ: {action!r}", hint="add · promote · demote · edit")
        entry = {"at": now, "by": by, "action": action}
        if payload.get("reason"):
            entry["reason"] = str(payload["reason"])[:200]
        fact.setdefault("history", []).append(entry)
        self._save(facts)
        return fact

    def propose(
        self, text: str, *, module: str, evidence: Sequence[str], by: str, initial_conf: float
    ) -> dict[str, Any]:
        """Learner: fact candidate kèm bằng chứng. Trùng nội dung + module với fact đã có → gộp bằng chứng."""
        facts = self._load()
        now = self._clock.now().isoformat()
        key = (" ".join(text.lower().split()), module)
        same = next(
            (f for f in facts if (" ".join(str(f.get("text", "")).lower().split()), f.get("module")) == key),
            None,
        )
        if same is not None:
            ev = list(dict.fromkeys([*same.get("evidence", []), *evidence]))
            same["evidence"] = ev[-20:]
            same.setdefault("history", []).append({"at": now, "by": by, "action": "evidence"})
            self._save(facts)
            return same
        fact: dict[str, Any] = {
            "id": f"F{len(facts) + 1:04d}",
            "text": text[:MAX_TEXT],
            "module": module or "*",
            "status": "candidate",
            "source": "learner",
            "created_by": by,
            "created_at": now,
            "evidence": list(dict.fromkeys(evidence))[-20:],
            "confidence": initial_conf,
            "applied": 0,
            "passed": 0,
            "history": [{"at": now, "by": by, "action": "add"}],
        }
        facts.append(fact)
        self._save(facts)
        return fact

    def record_use(self, refs: Iterable[str], *, issue: str, verdict: Verdict, cfg: MemoryCfg) -> list[str]:
        """Issue đã có fact Learner trong brief kết thúc → chấm điểm. Fact của người không bị harness đụng tới."""
        ids = {r.removeprefix("fact:") for r in refs if r.startswith("fact:")}
        if not ids:
            return []
        facts = self._load()
        now = self._clock.now().isoformat()
        changes: list[str] = []
        for f in facts:
            if f.get("id") not in ids or f.get("source") != "learner" or f.get("status") == "retired":
                continue
            conf, applied, passed = score(
                float(f.get("confidence", cfg.promote_initial_conf)),
                int(f.get("applied", 0)),
                int(f.get("passed", 0)),
                verdict,
                cfg,
            )
            status = next_status(str(f.get("status")), conf, applied, passed, cfg)
            f.update(confidence=round(conf, 3), applied=applied, passed=passed)
            if status != f.get("status"):
                f.setdefault("history", []).append(
                    {
                        "at": now,
                        "by": "harness",
                        "action": status,
                        "reason": f"{issue}: {passed}/{applied} pass",
                    }
                )
                changes.append(f"{f['id']}: {f.get('status')} → {status}")
                f["status"] = status
        self._save(facts)
        return changes

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        data = jsonio.loads(self._path.read_text(encoding="utf-8"))
        return [dict(f) for f in data.get("facts", []) if isinstance(f, dict)]

    def _save(self, facts: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self._path, jsonio.dumps({"facts": facts}), fsync=False)


def fact_lines(store: FactStore, module: str, *, candidates: int) -> list[tuple[str, str]]:
    """(dòng hiển thị, ref): fact active đúng module, rồi tối đa `candidates` fact candidate có nhãn."""
    out: list[tuple[str, str]] = []
    rows = [f for f in store.list_facts() if f.get("module") in (module, "*", "")]
    for f in rows:
        if f.get("status") != "active":
            continue
        where = f" (module {f['module']})" if f.get("module") not in ("*", "") else ""
        out.append((f"- {f['text']}{where}", f"fact:{f['id']}"))
    for f in [f for f in rows if f.get("status") == "candidate" and f.get("source") == "learner"][
        :candidates
    ]:
        out.append((f"- [unverified] {f['text']}", f"fact:{f['id']}"))
    return out


class FactsMemory:
    """MemoryProvider: fact active của module issue → khối RELEVANT EXPERIENCE trong brief."""

    def __init__(self, layout: HomeLayout) -> None:
        self._store = FactStore(layout)

    def block(
        self, role: str, issue_id: str, facts: Mapping[str, Any], *, max_tokens: int
    ) -> tuple[str, tuple[str, ...]]:
        intake = facts.get("intake") if isinstance(facts.get("intake"), Mapping) else {}
        module = str((intake or {}).get("affected_area") or "")
        lines = [
            "## RELEVANT EXPERIENCE (facts confirmed by the team, follow unless the spec says otherwise)"
        ]
        refs: list[str] = []
        for line, ref in fact_lines(self._store, module, candidates=0):
            if estimate_tokens("\n".join([*lines, line])) > max_tokens:
                break
            lines.append(line)
            refs.append(ref)
        return ("\n".join(lines), tuple(refs)) if refs else ("", ())

    def learned_skills(
        self, role: str, issue_id: str, facts: Mapping[str, Any]
    ) -> list[tuple[str, str, int]]:
        return []


__all__ = ["STATUSES", "FactStore", "FactsMemory", "fact_lines"]

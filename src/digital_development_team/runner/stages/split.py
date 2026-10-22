"""SPLIT — 0 LLM. Planner thấy issue quá lớn cho một thay đổi → plan.subtasks (2–5 phần) → mỗi phần thành issue con.

- Issue con: tiêu đề `<cha> — i/n: <phần>`, body = mô tả phần + phạm vi file + trích issue cha; blocked_by theo
  depends_on (chỉ số phần), cùng nhãn/độ ưu tiên với cha + nhãn `split`. raw.split_from = cha → con không tách tiếp.
- Issue nào đang chờ cha (blocked_by) chuyển sang chờ mọi issue con (Queue.replace_blocker).
- Idempotent: source_id con = `<source_id cha>#<i>` → chạy lại không tạo trùng.
Cha kết thúc DONE không merge (facts.split); worktree của cha (nếu có) không dùng.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import StageResult
from digital_development_team.ingest.base import RawItem
from digital_development_team.ingest.pipeline import ingest_items
from digital_development_team.ingest.queue import Queue
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.base import result
from digital_development_team.runner.state import IssueState

MAX_PARTS = 5
PARENT_EXCERPT = 1500


def _child_body(sub: Mapping[str, Any], i: int, n: int, parent: str, item: Mapping[str, Any]) -> str:
    files = [str(f) for f in sub.get("files") or []]
    lines = [
        str(sub.get("description") or "").strip(),
        "",
        f"Part {i} of {n}, split from {parent} ({str(item.get('title') or '')[:120]}).",
    ]
    if files:
        lines.append("Files in scope: " + ", ".join(files[:8]))
    body = str(item.get("body") or "").strip()
    if body:
        lines += ["", f"Original issue {parent} (excerpt):", body[:PARENT_EXCERPT]]
    return "\n".join(lines).strip()


class SplitStage:
    stage = Stage.SPLIT
    uses_llm = False

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        svc = ctx.svc
        issue = state.issue_id
        try:
            plan = svc.store.read(issue, "plan.yaml").content
            item = svc.store.read(issue, "item.json").content
        except StoreError as e:
            return result(self.stage, split={"created": 0, "error": e.message})
        subs = [s for s in plan.get("subtasks") or [] if isinstance(s, Mapping)][:MAX_PARTS]
        if len(subs) < 2 or (item.get("raw") or {}).get("split_from"):
            return result(self.stage, split={"created": 0, "error": "không có ≥2 phần, hoặc đã là issue con"})
        n = len(subs)
        source, sid = str(item.get("source") or "split"), str(item.get("source_id") or issue)
        raws = []
        for i, sub in enumerate(subs, 1):
            deps = [int(d) for d in sub.get("depends_on") or [] if isinstance(d, int) and 1 <= d < i]
            raws.append(
                RawItem(
                    source=source,
                    source_id=f"{sid}#{i}",
                    title=f"{str(item.get('title') or issue)[:80]} — {i}/{n}: {str(sub.get('title') or '')[:100]}",
                    body=_child_body(sub, i, n, issue, item),
                    labels=(*[str(x) for x in item.get("labels") or []], "split"),
                    priority_hint=item.get("priority_hint"),
                    blocked_by=tuple(f"{sid}#{d}" for d in deps),
                    raw={
                        "split_from": issue,
                        "part": i,
                        "of": n,
                        "files": [str(f) for f in sub.get("files") or []],
                    },
                )
            )
        queue = Queue(svc.db, svc.clock)
        rep = ingest_items(raws, store=svc.store, queue=queue, clock=svc.clock, source=source)
        children = [queue.find_by_source(source, r.source_id) for r in raws]
        ids = [c for c in children if c]
        if len(ids) < 2:
            return result(self.stage, split={"created": len(ids), "error": "; ".join(rep.errors)[:300]})
        moved = queue.replace_blocker(issue, ids)
        svc.sink.emit(
            EventDraft(
                issue_id=issue,
                actor="runner",
                event=EventType.ISSUE_SPLIT,
                stage=self.stage,
                data={"children": ids, "dependents_moved": list(moved)},
            )
        )
        return result(
            self.stage, split={"created": len(ids), "children": ids, "dependents_moved": list(moved)}
        )


__all__ = ["SplitStage"]

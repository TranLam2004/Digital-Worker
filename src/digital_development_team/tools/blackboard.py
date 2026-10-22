"""blackboard.* — read (ACL + projection) · write (schema, ≤3 lần/slot, patch/ lấy diff từ git) · list."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from digital_development_team.blackboard.store import FileStore
from digital_development_team.core.context import ToolContext
from digital_development_team.core.errors import IsolationError, SchemaError, SlotPermissionError, StoreError
from digital_development_team.core.models import PatchBundle, ToolResult
from digital_development_team.isolation.git import Git
from digital_development_team.policy.hooks import diff_sanity
from digital_development_team.tools.base import Handler
from digital_development_team.tools.harness_fields import HarnessFields

BaseResolver = Callable[[ToolContext], str]
"""Trả base commit (ref) để diff patch: Runner cấp (base_commit trong checkpoint), mặc định 'main'."""


class BlackboardTools:
    def __init__(
        self,
        store: FileStore,
        *,
        git: Git | None = None,
        base_ref: BaseResolver | None = None,
        max_attempts: int = 3,
        harness_fields: HarnessFields | None = None,
    ) -> None:
        self._store = store
        self.harness_fields = harness_fields or HarnessFields()
        self._git = git
        self._base_ref = base_ref or (lambda ctx: "main")
        self._max = max_attempts
        self._attempts: dict[tuple[str, str], int] = {}
        self.counts: Counter[str] = Counter()

    def handlers(self) -> dict[str, Handler]:
        return {"read": self.read, "write": self.write, "list": self.list}

    def attempts(self, issue: str, slot: str) -> int:
        return self._attempts.get((issue, slot), 0)

    # ───── read ─────
    def read(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        slot = str(args.get("slot", ""))
        version = args.get("version")
        field = args.get("field")
        try:
            art = self._store.read_as(
                ctx.issue_id, slot, ctx.actor, version=int(version) if version else None
            )
        except SlotPermissionError as e:
            return ToolResult.denied(
                "slot_acl",
                e.message,
                hint="Chỉ slot trong reads[] của bạn. Cần dữ liệu khác → ghi trong rationale, không đọc lén.",
            )
        except StoreError as e:
            return ToolResult.fail("NOT_FOUND", e.message, hint=e.hint)
        content: Any = art.content
        if field:
            if field not in art.content:
                return ToolResult.fail(
                    "NOT_FOUND", f"{slot} không có field {field!r}", hint=f"có: {sorted(art.content)[:12]}"
                )
            content = {field: art.content[field]}
        keys = ", ".join(sorted(content)[:6]) if isinstance(content, dict) else ""
        return ToolResult.ok(
            f"read {slot} v{art.ref.version} → {keys}",
            slot=slot,
            version=art.ref.version,
            sha256=art.ref.sha256,
            written_by=art.ref.written_by,
            written_at=art.ref.written_at.isoformat(),
            content=content,
        )

    # ───── write ─────
    def write(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        slot = str(args.get("slot", ""))
        content = args.get("content")
        if not isinstance(content, Mapping):
            return ToolResult.fail(
                "BAD_ARGS", "content phải là object", hint="Gửi artifact dạng JSON object, không phải chuỗi"
            )
        key = (ctx.issue_id, slot)
        n = self._attempts.get(key, 0) + 1
        self._attempts[key] = n
        if n > self._max:
            return ToolResult.fail(
                "TOO_MANY_ATTEMPTS",
                f"{slot}: đã thử {n - 1}/{self._max} lần",
                hint="Dừng. Trả {status: blocked, reason} nêu lỗi schema không sửa được.",
            )
        content = normalize_slot(slot, content)
        payload: Mapping[str, Any] | PatchBundle = self.harness_fields.merge(ctx.issue_id, slot, content)
        if slot == "patch/":
            built = self._build_patch(content, ctx)
            if isinstance(built, ToolResult):
                return built
            payload = built
        try:
            ref = self._store.write(ctx.issue_id, slot, payload, actor=ctx.actor)
        except SlotPermissionError as e:
            return ToolResult.denied("slot_acl", e.message, hint="Chỉ ghi slot trong writes[] của bạn.")
        except SchemaError as e:
            details = e.brief(5)
            return ToolResult(
                status="error",
                summary=f"write {slot} ✗ schema: {len(e.violations)} lỗi (lần {n}/{self._max})",
                structured={"attempt": n, "max": self._max},
                error=__import__("digital_development_team.core.models", fromlist=["ToolError"]).ToolError(
                    code="SCHEMA",
                    message=f"{len(e.violations)} lỗi",
                    hint=f"Sửa các field rồi write lại. Lần thử: {n}/{self._max}",
                    details=details,
                ),
            )
        except StoreError as e:
            return ToolResult.fail("STORE", e.message, hint=e.hint)
        self.counts[slot] += 1
        return ToolResult.ok(
            f"write {slot} v{ref.version} → ok (sha {ref.sha256[:8]})",
            slot=slot,
            version=ref.version,
            sha256=ref.sha256,
            path=ref.path,
        )

    def _build_patch(self, content: Mapping[str, Any], ctx: ToolContext) -> PatchBundle | ToolResult:
        """patch/: agent chỉ gửi impl_log (+handoff). diff · files_touched · base/head từ git worktree."""
        if self._git is None or not ctx.worktree:
            return ToolResult.fail(
                "NO_WORKTREE", "patch/ cần worktree + git", hint="Stage này không có worktree"
            )
        wt = Path(ctx.worktree)
        base = self._base_ref(ctx)
        try:
            head = self._git.head(cwd=wt)
            diff = self._git.diff_worktree(cwd=wt, base=base)
            changed = sorted(
                {p for _, p in self._git.status_porcelain(cwd=wt)}
                | set(self._git.changed_files(cwd=wt, base=base))
            )
        except IsolationError as e:
            return ToolResult.fail("GIT", e.message, hint="git.status để xem worktree")
        try:
            bundle = PatchBundle(
                diff=diff,
                files_touched=tuple(changed),
                impl_log=str(content.get("impl_log", ""))[:2000],
                base_commit=base if len(base) >= 7 else self._git.rev_parse(base, cwd=wt),
                head_commit=head,
                handoff=(str(content["handoff"])[:300] if content.get("handoff") else None),
            )
        except ValueError as e:
            return ToolResult.fail("BAD_ARGS", f"patch bundle không hợp lệ: {e}")
        problems = diff_sanity(bundle, ctx.allowed_files)
        if problems:
            return ToolResult.fail(
                "DIFF_SANITY",
                "; ".join(problems),
                hint="Patch rỗng/chỉ comment/ngoài allowlist → sửa rồi write lại",
                problems=problems,
            )
        return bundle

    # ───── list ─────
    def list(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        refs = self._store.list(ctx.issue_id)
        rows = [{"slot": r.slot, "version": r.version, "sha": r.sha256[:8], "by": r.written_by} for r in refs]
        return ToolResult.ok(f"list → {len(rows)} slot", slots=rows)


def normalize_slot(slot: str, content: Mapping[str, Any]) -> Mapping[str, Any]:
    """Sửa các lệch hình dạng mà model hay mắc, KHÔNG đổi nghĩa, trước khi validate schema.

    plan.yaml: `candidates[].files` là chuỗi ("shop/cart.py") → `{"path": "shop/cart.py"}` (gặp 2/2 lần chạy thật).
    """
    if slot != "plan.yaml" or not isinstance(content.get("candidates"), list):
        return content
    fixed = []
    for cand in content["candidates"]:
        if isinstance(cand, Mapping) and isinstance(cand.get("files"), list):
            files = [{"path": f} if isinstance(f, str) else f for f in cand["files"]]
            fixed.append({**cand, "files": files})
        else:
            fixed.append(cand)
    return {**content, "candidates": fixed}

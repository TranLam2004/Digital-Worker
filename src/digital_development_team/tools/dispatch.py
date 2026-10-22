"""Dispatcher — một tool call đi qua: policy.decide → tool.run → policy.after → artifactize → emit → stuck check.

Mọi side effect có provenance: actor · args_hash · approved_by · artifact_ref · duration · status (06-event-log).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from typing import Any

from digital_development_team.core.context import ToolContext
from digital_development_team.core.enums import EventType
from digital_development_team.core.errors import Cancelled, DDTError
from digital_development_team.core.events import EventDraft, ToolInfo
from digital_development_team.core.models import ToolCall, ToolResult
from digital_development_team.core.protocols import AgentSpecLike, EventSink
from digital_development_team.policy.approvals import ApprovalStore
from digital_development_team.policy.engine import PolicyContext, PolicyEngine
from digital_development_team.policy.hooks import redact_secrets
from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.base import ToolRegistry
from digital_development_team.tools.catalog import ToolCatalog
from digital_development_team.tools.stuck import StuckDetector, stopped_result, with_signal

log = logging.getLogger(__name__)
FS_READ_FACTOR = 3  # fs.read: trần artifactize ×3 (~6k token) — đủ một file ≤300 dòng


class Dispatcher:
    def __init__(
        self,
        catalog: ToolCatalog,
        registry: ToolRegistry,
        policy: PolicyEngine,
        sink: EventSink,
        artifacts: ArtifactStore,
        *,
        artifactize_threshold: int = 2000,
    ) -> None:
        self._catalog = catalog
        self._registry = registry
        self._policy = policy
        self._sink = sink
        self._artifacts = artifacts
        self._threshold = artifactize_threshold
        self._counts: dict[str, Counter[str]] = {}
        self._approvals = ApprovalStore(artifacts.layout)  # G2 người đã duyệt (policy/approvals.py)
        self._stuck = StuckDetector()

    def counts(self, issue: str) -> Mapping[str, int]:
        return self._counts.get(issue, Counter())

    def begin_run(self, issue: str, actor: str) -> None:
        """Đầu mỗi lần chạy agent: bộ phát hiện kẹt đếm lại từ đầu."""
        self._stuck.reset(f"{issue}:{actor}")

    def dispatch(
        self, call: ToolCall, spec: AgentSpecLike, ctx: ToolContext, pctx: PolicyContext
    ) -> ToolResult:
        actor = f"{call.actor.role}:{call.actor.sub_id}" if call.actor.sub_id else call.actor.role
        key = f"{call.issue_id}:{actor}"
        if self._stuck.stopped(key):  # đã báo dừng ở lượt này (CLI không ngắt được loop của model)
            return stopped_result()
        result = self._dispatch(call, spec, ctx, pctx, actor)
        sig = self._stuck.observe(key, call.name, call.args_hash(), result)
        if sig is None:
            return result
        self._sink.emit(
            EventDraft(
                issue_id=call.issue_id,
                actor=actor,
                event=EventType.STUCK_DETECTED,
                stage=call.stage,
                data={"kind": sig.kind, "tool": call.name, "stop": sig.stop},
            )
        )
        return with_signal(result, sig)

    def _dispatch(
        self, call: ToolCall, spec: AgentSpecLike, ctx: ToolContext, pctx: PolicyContext, actor: str
    ) -> ToolResult:
        issue_counts = self._counts.setdefault(call.issue_id, Counter())
        pctx_full = PolicyContext(
            tier=pctx.tier,
            worktree=pctx.worktree,
            allowed_files=pctx.allowed_files,
            spec_values=pctx.spec_values,
            issue_tool_counts=dict(issue_counts),
            autonomy=pctx.autonomy,
            approvals=self._approvals.active(call.issue_id),
        )
        decision = self._policy.decide(call, spec, pctx_full)
        issue_counts[call.name] += 1
        if not decision.allowed:
            result = decision.to_result()
            self._emit(
                call,
                actor,
                EventType.TOOL_DENIED,
                ToolInfo(
                    name=call.name,
                    risk_tier=decision.risk_tier,
                    denied_reason=decision.reason,
                    args_hash=call.args_hash(),
                    status="denied",
                ),
                {
                    "rule": decision.rule_id,
                    "layer": decision.layer,
                    **_target(call.args),
                    **(
                        {"gate": decision.gate, "approval_key": decision.extra.get("approval_key")}
                        if decision.gate
                        else {}
                    ),
                },
            )
            return result
        if decision.extra.get("scope") == "once":  # quyền G2 một lần: dùng xong là hết
            self._approvals.consume(call.issue_id, str(decision.extra.get("approval")), at=ctx.clock.now())

        handler = self._registry.handler(call.name)
        op = self._catalog.op(call.name)
        start = ctx.clock.monotonic()
        if handler is None:
            result = ToolResult.fail("NOT_IMPLEMENTED", f"{call.name} chưa có impl", hint="dùng tool khác")
        else:
            profile = _profile_suffix(spec, call.name)
            try:
                result = handler(call.args, ctx, profile)
            except Cancelled:
                raise
            except DDTError as e:
                result = ToolResult.fail(e.code, e.message, hint=e.hint)
            except Exception as e:
                log.exception("tool %s crash", call.name)
                result = ToolResult.fail(
                    "TOOL_CRASH", f"{type(e).__name__}: {e}"[:300], hint="báo P3; thử tool khác"
                )
        duration_ms = int((ctx.clock.monotonic() - start) * 1000)
        result = self._policy.after(call, result)
        if op is not None and op.artifactize:
            result = self._artifactize(call.issue_id, call.name, result)
        self._emit(
            call,
            actor,
            EventType.TOOL_CALL,
            ToolInfo(
                name=call.name,
                risk_tier=decision.risk_tier,
                approved_by=decision.approved_by,
                args_hash=call.args_hash(),
                duration_ms=duration_ms,
                status=result.status,
            ),
            {
                "rule": decision.rule_id,
                **_target(call.args),
                **({"error": result.error.code} if result.error else {}),
            },
            artifact_ref=result.artifact_refs[0] if result.artifact_refs else None,
        )
        return result

    # ───── nội bộ ─────
    def _artifactize(self, issue: str, tool: str, result: ToolResult) -> ToolResult:
        """structured có field text lớn (content/stdout/diff/output) → artifact + preview."""
        if result.artifact_refs:
            return result
        # fs.read đã tự giới hạn (≤300 dòng/khoảng dòng): cắt thành preview chỉ buộc agent đọc lại lần nữa
        threshold = self._threshold * FS_READ_FACTOR if tool == "fs.read" else self._threshold
        big_keys = [
            k
            for k, v in result.structured.items()
            if isinstance(v, str) and ArtifactStore.should_artifactize(v, threshold)
        ]
        if not big_keys:
            return result
        structured = dict(result.structured)
        refs: list[str] = []
        preview: tuple[str, ...] = result.preview
        for k in big_keys:
            text = structured[k]
            ref = self._artifacts.put(issue, text, ext="txt", label=f"{tool.replace('.', '_')}-{k}")
            refs.append(ref)
            structured[k] = f"<{len(text)} ký tự → {ref}>"
            if not preview:
                preview = ArtifactStore.preview(text)
        return result.model_copy(
            update={"structured": structured, "artifact_refs": tuple(refs), "preview": preview}
        )

    def _emit(
        self, call: ToolCall, actor: str, event: EventType, info: ToolInfo, data: dict[str, Any], **kw: Any
    ) -> None:
        self._sink.emit(
            EventDraft(
                issue_id=call.issue_id, actor=actor, event=event, stage=call.stage, tool=info, data=data, **kw
            )
        )


def _profile_suffix(spec: AgentSpecLike, name: str) -> str | None:
    for t in spec.tools:
        base, _, suffix = t.partition(":")
        if base == name and suffix:
            return suffix
    return None


def _target(args: Mapping[str, Any]) -> dict[str, Any]:
    """Đích của lời gọi để ghi vào events.jsonl. Che secret trước: audit log là vĩnh viễn (exec.tool.md)."""
    out: dict[str, Any] = {}
    for k in ("path", "slot", "cmd"):
        v = args.get(k)
        if isinstance(v, str):
            out[k] = redact_secrets(v)[0][:120]
    return out

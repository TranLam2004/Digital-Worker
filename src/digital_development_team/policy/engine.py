"""PolicyEngine.decide — 4 lớp, thứ tự cố định. Không LLM.

Lớp 1 hook: tool ∈ allowlist agent · kiểm lệnh exec/test (policy/command.py) · path_normalize · scope_check · git.push không tồn tại
Lớp 2 static rules: policy.yaml#tool_rules (deny list exec, secret files, sensitive, destructive → ask G2 …)
Lớp 3 permission mode: readonly → mọi soft/hard_write lên fs/exec deny (trừ blackboard.write slot của mình) ·
                       accept_edits → soft_write trong scope + hard_write sandbox auto · ask_all → ask
Lớp 4 human (G2): TODO(P2#16) — hiện `ask` không auto được → deny + hint escalation destructive_request
after(): redact secret trong result · injection_scan trên output ngoài → đánh dấu structured.injection_suspect
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from digital_development_team.config.models import PolicyConfig, SensitivePathsConfig
from digital_development_team.core.enums import PermissionMode, RiskTier, Tier
from digital_development_team.core.errors import PolicyDenied
from digital_development_team.core.models import ToolCall, ToolResult
from digital_development_team.core.protocols import AgentSpecLike
from digital_development_team.policy.approvals import approval_key
from digital_development_team.policy.command import CommandVerdict, check_command
from digital_development_team.policy.hooks import injection_scan, path_normalize, redact_secrets, scope_check
from digital_development_team.policy.netguard import NetVerdict, check_query, check_url
from digital_development_team.policy.rules import RuleContext, RuleTable

Action = Literal["allow", "deny", "ask"]


class OpInfo(Protocol):
    """Thông tin op mà policy cần — tools.catalog.ToolOp thoả (policy L4 không import tools L5)."""

    @property
    def risk_tier(self) -> RiskTier: ...

    @property
    def allowed_roles(self) -> list[str]: ...


class OpLookup(Protocol):
    def op(self, name: str) -> OpInfo | None: ...


_FS_WRITE_OPS = {"fs.write", "fs.edit", "fs.delete"}
_SHELL_TOOLS = {"exec.run", "test.run"}
_EXTERNAL_PREFIXES = ("web.", "docs.", "mcp.")
_EXTERNAL_OUTPUT_TOOLS = {
    "skill.load",
    "llm.ask",
    "fs.read",
    "fs.search",
    "exec.run",
    "test.run",
    "git.log",
    "git.diff",
    "security.scan",
    "codegraph.node",
    "codegraph.search",
}


@dataclass(frozen=True, slots=True)
class PolicyContext:
    tier: Tier
    worktree: str | None
    allowed_files: Sequence[str]
    spec_values: Mapping[str, str | None] = field(default_factory=dict)
    issue_tool_counts: Mapping[str, int] = field(default_factory=dict)
    autonomy: str = "balanced"
    approvals: Mapping[str, str] = field(
        default_factory=dict
    )  # G2 người đã duyệt: {approval_key: once|issue}


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    rule_id: str
    reason: str
    layer: int
    risk_tier: RiskTier
    hint: str | None = None
    constraint: Mapping[str, Any] = field(default_factory=dict)
    gate: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def approved_by(self) -> str:
        return {"allow": "policy", "deny": "policy", "ask": "pending"}[self.action]

    def to_result(self) -> ToolResult:
        extra = {k: v for k, v in self.extra.items() if k not in ("rule_id", "layer", "hint", "message")}
        return ToolResult.denied(self.rule_id, self.reason, hint=self.hint, **extra)


class PolicyEngine:
    def __init__(self, policy: PolicyConfig, sensitive: SensitivePathsConfig, catalog: OpLookup) -> None:
        self._policy = policy
        self._sensitive = sensitive
        self._catalog = catalog
        self._rules = RuleTable.from_policy(policy.tool_rules)

    # ───── decide ─────
    def decide(self, call: ToolCall, spec: AgentSpecLike, ctx: PolicyContext) -> Decision:
        tool = call.name
        op = self._catalog.op(tool)
        risk = op.risk_tier if op else RiskTier.HARD_WRITE

        # Lớp 1 — hook
        granted = {t.split(":", 1)[0] for t in spec.tools}
        if tool not in granted:
            return Decision(
                "deny",
                "allowlist",
                f"{tool} không trong allowlist của {spec.name}",
                1,
                risk,
                hint="Dùng tool khác trong danh sách, hoặc blocked nếu không thể.",
            )
        if op is None:
            return Decision("deny", "unknown_tool", f"tool {tool} không có trong catalog", 1, risk)
        if spec.name not in op.allowed_roles and "all" not in op.allowed_roles:
            return Decision("deny", "op_roles", f"{tool} không dành cho role {spec.name}", 1, risk)
        if tool in _SHELL_TOOLS:
            guard = self._command_guard(tool, call.args, spec, ctx)
            if guard is not None:
                return Decision("deny", f"cmd_guard:{guard.rule}", guard.reason, 1, risk, hint=guard.hint)
        net = self._network_guard(tool, call.args)
        if net is not None:
            return Decision(
                "deny",
                net.rule,
                net.reason,
                1,
                risk,
                hint="Only https pages on policy.network.allow; no secrets",
            )
        path = _path_arg(call.args)
        rel_path: str | None = None
        if tool.startswith("fs.") and path is not None and not path.startswith("artifact://"):
            if not ctx.worktree:
                return Decision(
                    "deny", "no_worktree", "chưa có worktree — fs.* không khả dụng ở stage này", 1, risk
                )
            try:
                from pathlib import Path

                _, rel_path = path_normalize(Path(ctx.worktree), path)
                if tool in _FS_WRITE_OPS:
                    scope_check(rel_path, ctx.allowed_files)
            except PolicyDenied as e:
                return Decision("deny", e.rule_id, e.message, 1, risk, hint=e.hint, extra=e.ctx)

        # Lớp 2 — static rules
        args = dict(call.args)
        if rel_path is not None:
            args["path"] = rel_path
        key = approval_key(tool, call.args, rel_path)
        rctx = RuleContext(
            tool=tool,
            role=spec.name,
            args=args,
            tier=ctx.tier,
            allowed_files=ctx.allowed_files,
            sensitive_globs=self._sensitive.globs(),
            spec_values=ctx.spec_values,
            issue_tool_count=ctx.issue_tool_counts.get(tool, 0),
        )
        hit = self._rules.first_match(rctx)
        rule = hit.rule
        if rule.action == "deny":
            return Decision(
                "deny",
                hit.rule_id,
                rule.reason or "static rule deny",
                2,
                risk,
                hint=_deny_hint(tool, rule.reason),
                gate=rule.gate,
            )
        if rule.action == "allow":
            return Decision(
                "allow", hit.rule_id, rule.reason or "static rule allow", 2, risk, constraint=rule.constraint
            )
        # ask → Lớp 3
        if rule.gate == "G2" or risk is RiskTier.DESTRUCTIVE:
            return self._g2(hit.rule_id, rule.reason or "destructive", risk, key=key, ctx=ctx)
        return self._permission_layer(tool, risk, spec, ctx, hit.rule_id, key=key)

    def _network_guard(self, tool: str, args: Mapping[str, Any]) -> NetVerdict | None:
        """web.fetch: URL (allowlist, https, không secret) · web.search/docs.lookup: truy vấn (độ dài, không secret)."""
        cfg = self._policy.network
        if tool == "web.fetch":
            v = check_url(str(args.get("url") or ""), cfg)
        elif tool == "web.search":
            v = check_query(str(args.get("query") or ""), cfg)
        elif tool == "docs.lookup":
            v = check_query(
                " ".join(str(args.get(k) or "") for k in ("package", "symbol", "question")).strip(), cfg
            )
            if not v.ok and v.rule == "net:query_too_long":
                return None  # docs.lookup tự dựng URL, độ dài question đã giới hạn bởi schema
        else:
            return None
        return None if v.ok else v

    def _command_guard(
        self, tool: str, args: Mapping[str, Any], spec: AgentSpecLike, ctx: PolicyContext
    ) -> CommandVerdict | None:
        """Lệnh sẽ chạy (cmd, hoặc spec.test_command khi test.run không truyền cmd) qua policy/command.py."""
        cmd = args.get("cmd")
        if not (isinstance(cmd, str) and cmd.strip()) and tool == "test.run":
            cmd = ctx.spec_values.get("spec.test_command")
        if not (isinstance(cmd, str) and cmd.strip()):
            return None
        suffix = next((t.split(":", 1)[1] for t in spec.tools if t.startswith(f"{tool}:")), "")
        readonly = spec.permission_mode is PermissionMode.READONLY or suffix == "readonly"
        verdict = check_command(
            cmd, self._policy.command_guard, readonly=readonly, scan=suffix == "scan", worktree=ctx.worktree
        )
        return None if verdict.ok else verdict

    def _permission_layer(
        self,
        tool: str,
        risk: RiskTier,
        spec: AgentSpecLike,
        ctx: PolicyContext,
        rule_id: str,
        *,
        key: str = "",
    ) -> Decision:
        mode = spec.permission_mode
        preset = self._policy.autonomy_presets.get(ctx.autonomy)
        if spec.name == "dev" and preset is not None:
            mode = preset.dev_permission  # autonomy dial dịch chuyển Dev
        if risk is RiskTier.READ:
            return Decision("allow", rule_id, "read — mặc định allow", 3, risk)
        if (
            tool.startswith("blackboard.")
            or tool.startswith("memory.")
            or tool in ("user.ask", "llm.ask", "agent.list", "agent.collect", "agent.stop", "agent.delegate")
        ):
            return Decision("allow", rule_id, "tool nội bộ harness — ACL riêng cưỡng chế", 3, risk)
        if mode is PermissionMode.READONLY and tool in ("test.run", "exec.run"):
            # readonly role vẫn CHẠY được trong sandbox readonly (Verifier chạy test = bằng chứng); fs write bị chặn
            return Decision(
                "allow",
                rule_id,
                "readonly — chạy trong sandbox readonly",
                3,
                risk,
                constraint={"readonly_fs": True, "network": False},
            )
        if mode is PermissionMode.READONLY or mode is PermissionMode.MEMORY_ONLY:
            return Decision(
                "deny",
                "permission_readonly",
                f"{spec.name} là {mode.value} — {tool} ({risk.value}) bị chặn",
                3,
                risk,
                hint="Role này chỉ đọc/chạy trong sandbox readonly. Ghi output qua blackboard.write.",
            )
        if mode is PermissionMode.ACCEPT_EDITS:
            if risk is RiskTier.SOFT_WRITE:
                return Decision("allow", rule_id, "accept_edits — soft_write trong scope", 3, risk)
            if risk is RiskTier.HARD_WRITE:
                return Decision(
                    "allow",
                    rule_id,
                    "accept_edits — hard_write trong sandbox",
                    3,
                    risk,
                    constraint={"cwd": "$WORKTREE", "network": False},
                )
        if mode is PermissionMode.ASK_ALL:
            return self._g2(rule_id, f"ask_all — {tool} cần người duyệt", risk, key=key, ctx=ctx)
        return self._g2(rule_id, f"{tool} ({risk.value}) cần người duyệt", risk, key=key, ctx=ctx)

    @staticmethod
    def _g2(
        rule_id: str, reason: str, risk: RiskTier, *, key: str = "", ctx: PolicyContext | None = None
    ) -> Decision:
        """Lớp 4. Người đã duyệt đúng lời gọi này (approvals.json) → cho qua; chưa → chặn kèm khoá để xin duyệt."""
        scope = (ctx.approvals if ctx is not None else {}).get(key) if key else None
        if scope:
            return Decision(
                "allow",
                "g2_approved",
                f"người đã duyệt G2 ({'một lần' if scope == 'once' else 'cả issue'})",
                4,
                risk,
                gate="G2",
                extra={"approval": key, "scope": scope},
            )
        return Decision(
            "deny",
            rule_id,
            f"G2: {reason}",
            4,
            risk,
            gate="G2",
            hint=(
                "Cần người duyệt G2. Ghi escalation.yaml type=destructive_request nêu đúng lời gọi này và lý do, rồi DỪNG. "
                "Người cho phép thì bạn chạy lại đúng lời gọi đó."
            ),
            extra={"approval_key": key},
        )

    # ───── after ─────
    def after(self, call: ToolCall, result: ToolResult) -> ToolResult:
        changed = False
        summary, n1 = redact_secrets(result.summary)
        # đệ quy: stdout_tail, matches… là list/dict; chỉ redact chuỗi tầng đầu từng để lộ key thật
        redacted_structured, n2 = _redact_deep(dict(result.structured))
        structured: dict[str, Any] = dict(redacted_structured)
        preview = []
        n3 = 0
        for line in result.preview:
            nl, k3 = redact_secrets(line)
            preview.append(nl)
            n3 += k3
        if n1 or n2 or n3:
            structured["redacted"] = n1 + n2 + n3
            changed = True
        if (
            call.name in _EXTERNAL_OUTPUT_TOOLS or call.name.startswith(_EXTERNAL_PREFIXES)
        ) and result.status == "ok":
            blob = "\n".join(_strings(structured)) + "\n" + "\n".join(preview)
            sus = injection_scan(blob)
            if sus:
                structured["injection_suspect"] = sus
                changed = True
        if not changed:
            return result
        return result.model_copy(
            update={"summary": summary[:300], "structured": structured, "preview": tuple(preview)}
        )


def _redact_deep(value: Any) -> tuple[Any, int]:
    """redact_secrets trên mọi chuỗi trong dict/list lồng nhau. Trả (giá trị mới, số lần che)."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        total = 0
        out: dict[Any, Any] = {}
        for k, v in value.items():
            out[k], n = _redact_deep(v)
            total += n
        return out, total
    if isinstance(value, (list, tuple)):
        items = [_redact_deep(v) for v in value]
        seq = [v for v, _ in items]
        return (tuple(seq) if isinstance(value, tuple) else seq), sum(n for _, n in items)
    return value, 0


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _path_arg(args: Mapping[str, Any]) -> str | None:
    for k in ("path", "file", "target"):
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _deny_hint(tool: str, reason: str | None) -> str:
    if tool.startswith("exec.") or tool.startswith("test."):
        return "Lệnh khớp deny list. Dùng lệnh khác, không lặp lại y nguyên."
    if tool.startswith("fs."):
        return "File này bị chặn bởi policy. Cần đụng → escalation type=risk/destructive_request."
    return reason or "policy deny"

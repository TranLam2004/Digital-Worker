"""AgentLoop — một lần chạy agent = process mới, context sạch (sync + thread, xem README › Quyết định kiến trúc).

    tools CỐ ĐỊNH cả phiên · system prompt cố định (cache) · brief = user message đầu
    while: cancel? → budget? (hết → 1 lượt cuối APPEND, không sửa system) → provider.chat → emit llm_call
           → refusal → blocked · không tool call → (final_message: tách JSON → store.write) | (tool_write: nhắc 1 lần)
           → mỗi tool call: Dispatcher (policy → run → after → emit) → tool_result
           → output slot đã ghi → return NGAY · escalation.yaml ghi → return escalation
Condenser tầng 0–1 (structured + artifactize) ở Dispatcher; tầng 2 (thu gọn tool_result cũ) ở đây qua condenser/. Tầng 3 (tóm tắt bằng LLM): chưa làm.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from digital_development_team.agents.brief import render_brief
from digital_development_team.agents.outcome import blocked_reason, extract_json_object
from digital_development_team.agents.prompt import compose_system_prompt
from digital_development_team.agents.skills import SkillRegistry
from digital_development_team.agents.spec import AgentRegistry, AgentSpec
from digital_development_team.blackboard.store import FileStore
from digital_development_team.budget.meter import BudgetMeter
from digital_development_team.condenser import condense_tool_results
from digital_development_team.config.models import BudgetsConfig, PolicyConfig
from digital_development_team.config.resources import AssetResolver
from digital_development_team.core.context import RunContext, ToolContext
from digital_development_team.core.enums import EventType, OutputMode
from digital_development_team.core.errors import (
    ConfigError,
    ProviderError,
    ProviderPaused,
    SchemaError,
    SlotPermissionError,
    StoreError,
)
from digital_development_team.core.events import BudgetSnapshot, EventDraft, LlmInfo
from digital_development_team.core.llm import ChatRequest, Message, StopReason, ToolResultBlock, ToolSpec
from digital_development_team.core.models import AgentOutcome, Brief, ToolCall, ToolResult
from digital_development_team.core.protocols import AgentSpecLike, EventSink
from digital_development_team.eventlog.transcript import TranscriptWriter
from digital_development_team.llm.registry import LLMClient, ModelRegistry, Resolution
from digital_development_team.llm.toolnames import to_canonical
from digital_development_team.policy.engine import PolicyContext
from digital_development_team.policy.hooks import redact_secrets
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.tools.catalog import ToolCatalog
from digital_development_team.tools.dispatch import Dispatcher
from digital_development_team.util.hashing import sha256_text

log = logging.getLogger(__name__)

FINAL_TURN_MSG = (
    "⚠ The budget for this stage is exhausted. This is your FINAL turn: write the output now (blackboard_write) "
    'if you have enough, or return {"status": "blocked", "reason": "<why>"}. Do not call any more read tools.'
)
NUDGE_MSG = (
    "You ended the turn without writing `{slot}`. Call blackboard_write for that slot, "
    'or return {{"status": "blocked", "reason": "<specific reason>"}}.'
)
SCHEMA_RETRY_MSG = "Output does not match the schema ({n}/{max}). Errors:\n{errors}\nReturn EXACTLY one corrected JSON object."
NOT_JSON_MSG = (
    "The reply must be EXACTLY one JSON object matching the OUTPUT CONTRACT (no markdown, no explanation)."
)
MAX_FINAL_ATTEMPTS = 3


class AgentLoop:
    def __init__(
        self,
        *,
        models: ModelRegistry,
        catalog: ToolCatalog,
        dispatcher: Dispatcher,
        store: FileStore,
        sink: EventSink,
        resolver: AssetResolver,
        agents: AgentRegistry,
        skills: SkillRegistry,
        budgets: BudgetsConfig,
        policy: PolicyConfig,
        home: Path,
        max_tokens: int = 8192,
        repo: Path | None = None,
        config_dir: Path | None = None,
        preset: str | None = None,
        flags: Sequence[str] = (),
    ) -> None:
        self._repo = repo
        self._config_dir = config_dir
        self._preset = preset
        self._flags = tuple(flags)
        self._models = models
        self._catalog = catalog
        self._dispatcher = dispatcher
        self._store = store
        self._sink = sink
        self._resolver = resolver
        self._agents = agents
        self._skills = skills
        self._budgets = budgets
        self._policy = policy
        self._home = home
        self._max_tokens = max_tokens

    # ───── public ─────
    def run(self, spec_like: AgentSpecLike, brief: Brief, ctx: RunContext) -> AgentOutcome:
        spec: AgentSpec = spec_like if isinstance(spec_like, AgentSpec) else self._agents.get(spec_like.name)
        tw = self._transcript(ctx)
        model_role = ctx.model_role or spec.name
        try:
            client = self._models.client(model_role)
        except ConfigError as e:  # role nâng cấp chưa cấu hình / không khả dụng → model thường
            log.warning("%s: không dùng được %s (%s) → %s", spec.name, model_role, e.message, spec.name)
            model_role, client = spec.name, self._models.client(spec.name)
        out = self._run_once(spec, brief, ctx, client, tw)
        if model_role != spec.name and out.kind == "blocked" and _model_unavailable(out.reason):
            # model mạnh hơn không chạy được trên transport này (vd CLI subscription không có) → model thường
            log.warning("%s: %s không khả dụng (%s) → %s", spec.name, model_role, out.reason, spec.name)
            if tw is not None:
                tw.write("note", text=f"{model_role} unavailable, falling back to {spec.name}")
            out = self._run_once(spec, brief, ctx, self._models.client(spec.name), tw)
        if tw is not None:
            tw.write(
                "outcome",
                result=out.kind,
                uri=out.artifact_uri or out.escalation_uri,
                reason=out.reason,
                usage=out.usage.model_dump(),
            )
        return out

    def _run_once(
        self, spec: AgentSpec, brief: Brief, ctx: RunContext, client: LLMClient, tw: TranscriptWriter | None
    ) -> AgentOutcome:
        provider = self._models.provider(client.ref)
        if provider.caps.agent_loop and hasattr(provider, "run_agent") and self._multi_turn(spec):
            return self._run_cli_agent(spec, brief, ctx, client, provider, tw)
        return self._run_with_client(spec, brief, ctx, client, tw)

    def _multi_turn(self, spec: AgentSpec) -> bool:
        """Vai làm việc nhiều lượt (hồ sơ chính tool_write: Planner, Dev, Verifier, Security) → agent loop CLI ở mọi
        stage mode, kể cả mode final_message như Dev ở ANSWER: `claude -p` một call bỏ hết tool, agent không đọc được code.
        Vai một call (Intake, Analyst, Leader) giữ chat một call. Vai phải hành động qua tool ghi (Learner ghi memory.*)
        cũng cần loop, dù đầu ra là final_message."""
        if spec.output_mode is OutputMode.TOOL_WRITE:
            return True
        if any(t.startswith("memory.") and t not in _MEMORY_READ for t in spec.tool_names):
            return True
        try:
            return self._agents.get(spec.name).output_mode is OutputMode.TOOL_WRITE
        except ConfigError:
            return False

    def _transcript(self, ctx: RunContext) -> TranscriptWriter | None:
        """Transcript cho người xem quá trình (policy.transcripts). Đã redact; lỗi I/O không chặn agent."""
        if not getattr(self._policy, "transcripts", True):
            return None
        stage = ctx.stage.value + (f".{ctx.actor.role}-{ctx.actor.sub_id}" if ctx.actor.sub_id else "")
        return TranscriptWriter.for_run(
            HomeLayout(self._home),
            ctx.issue_id,
            stage,
            ctx.attempt,
            redact=lambda s: redact_secrets(s)[0],
            clock=ctx.clock,
        )

    def _run_with_client(
        self,
        spec: AgentSpec,
        brief: Brief,
        ctx: RunContext,
        client: LLMClient,
        tw: TranscriptWriter | None = None,
    ) -> AgentOutcome:
        """Transport API: harness chạy loop, mỗi lượt một Complete."""
        issue = ctx.issue_id
        slot = spec.output_slot
        system = compose_system_prompt(spec, self._agents, self._skills, self._resolver)
        tools: tuple[ToolSpec, ...] = self._catalog.specs_for(spec.tool_names)
        messages: list[Message] = [Message.user_text(render_brief(brief, self._resolver))]
        if tw is not None:
            tw.write(
                "system",
                role=spec.name,
                hash=sha256_text(system),
                chars=len(system),
                tools=[t.name for t in tools] if spec.tools else [],
                model=client.ref.model,
                transport="api",
            )
            tw.write("brief", text=messages[0].text)
        meter = BudgetMeter(ctx.budget, ctx.clock)
        base_out = self._version(issue, slot)
        base_esc = self._version(issue, "escalation.yaml")
        base_alt = {s: self._version(issue, s) for s in _alt_slots(spec)}
        spec_vals = self._spec_values(issue)
        tool_ctx = ToolContext(
            issue_id=issue,
            actor=ctx.actor,
            stage=ctx.stage,
            worktree=ctx.worktree,
            home=str(self._home),
            allowed_files=ctx.allowed_files,
            cancel=ctx.cancel,
            clock=ctx.clock,
        )
        pctx = PolicyContext(
            tier=ctx.tier,
            worktree=ctx.worktree,
            allowed_files=ctx.allowed_files,
            spec_values=spec_vals,
            autonomy=self._policy.autonomy,
        )
        prompt_hash = sha256_text(system + messages[0].text)
        self._dispatcher.begin_run(issue, _actor_key(ctx))
        caps = self._models.provider(client.ref).caps
        condense_at = spec.condenser.evict_at * caps.max_context_tokens
        final_turn_sent = False
        nudged = False
        final_attempts = 0
        repairing = False  # final_message: lượt sửa schema không tính trục turns (token/wall vẫn cap)

        while True:
            ctx.cancel.raise_if_cancelled()
            # ── budget ──
            axis = meter.exceeded_axis()
            if axis == "turns" and repairing and final_attempts < MAX_FINAL_ATTEMPTS:
                axis = None
            if axis is not None:
                if final_turn_sent:
                    self._emit(
                        ctx,
                        EventType.CIRCUIT_BREAKER,
                        {"axis": meter.exceeded_axis(), "scope": "stage"},
                        budget=meter,
                    )
                    return AgentOutcome.blocked(f"hết budget stage ({meter.exceeded_axis()})", meter.usage)
                messages.append(Message.user_text(FINAL_TURN_MSG))
                final_turn_sent = True
                if tw is not None:
                    tw.write("harness", turn=meter.turns, text=FINAL_TURN_MSG)
            for lv in meter.crossed():
                self._emit(ctx, EventType.BUDGET_WARNING, {"level": lv, "scope": "stage"}, budget=meter)
                msg = self._budgets.warnings.get(f"at_{lv}", {}).get("agent_message")
                if msg:
                    messages.append(Message.user_text(str(msg)))
                    if tw is not None:
                        tw.write("harness", turn=meter.turns, text=str(msg))

            # ── chat ──
            req = ChatRequest(
                model=client.ref,
                system=system,
                messages=tuple(messages),
                tools=tools if spec.tools else (),
                max_tokens=self._max_tokens,
                metadata={"role": spec.name, "issue": issue, "stage": ctx.stage.value},
            )
            try:
                out = client.chat(req, cancel=ctx.cancel)
            except ProviderError as e:
                if (
                    e.retryable
                ):  # LLMClient đã retry + fallback → provider đang sập/hết hạn mức: tạm dừng, không chặn
                    raise ProviderPaused(
                        f"provider tạm thời lỗi: {e.message}",
                        hint="chạy lại `ddt run <issue>` khi provider hồi phục (issue giữ nguyên stage)",
                    ) from e
                self._emit(ctx, EventType.BLOCKED, {"reason": f"provider: {e.message}"}, budget=meter)
                return AgentOutcome.blocked(f"provider lỗi: {e.message}", meter.usage)
            resp = out.response
            meter.add_turn(
                resp.usage.tokens_in,
                resp.usage.tokens_out,
                cached=resp.usage.tokens_cached,
                cost_usd=out.cost_usd,
            )
            self._emit(
                ctx,
                EventType.LLM_CALL,
                {
                    "stop": resp.stop.value,
                    "tool_calls": len(resp.tool_calls),
                    "turn": meter.turns,
                    "fell_back": out.fell_back,
                },
                llm=LlmInfo(
                    model=out.ref.model,
                    tokens_in=resp.usage.tokens_in,
                    tokens_out=resp.usage.tokens_out,
                    tokens_cached=resp.usage.tokens_cached,
                    latency_ms=resp.usage.latency_ms,
                    cost_usd=out.cost_usd,
                    prompt_hash=prompt_hash,
                    transport=self._models.transport_of(out.ref.provider),
                ),
                budget=meter,
            )
            messages.append(resp.message)
            if tw is not None:
                tw.write(
                    "assistant",
                    turn=meter.turns,
                    model=out.ref.model,
                    stop=resp.stop.value,
                    text=resp.text,
                    tool_uses=[{"id": b.id, "name": b.name, "input": b.input} for b in resp.tool_calls],
                    usage={
                        "tokens_in": resp.usage.tokens_in,
                        "tokens_out": resp.usage.tokens_out,
                        "tokens_cached": resp.usage.tokens_cached,
                        "latency_ms": resp.usage.latency_ms,
                        "cost_usd": out.cost_usd,
                    },
                )
            if resp.stop is StopReason.REFUSAL:
                return AgentOutcome.blocked("model từ chối (refusal)", meter.usage)

            calls = resp.tool_calls
            if not calls:
                text = resp.text
                obj = extract_json_object(text)
                reason = blocked_reason(obj, text)
                if reason:
                    return AgentOutcome.blocked(reason, meter.usage)
                if spec.output_mode is OutputMode.FINAL_MESSAGE:
                    final_attempts += 1
                    done = self._write_final(spec, issue, obj, messages, final_attempts, tw=tw)
                    repairing = done is None
                    if done is not None:
                        return (
                            AgentOutcome.artifact(done, meter.usage)
                            if done
                            else AgentOutcome.blocked("output không đúng schema sau nhiều lần", meter.usage)
                        )
                    continue
                # tool_write: đã ghi chưa?
                v_out, v_esc = self._version(issue, slot), self._version(issue, "escalation.yaml")
                if v_out > base_out:
                    return AgentOutcome.artifact(f"blackboard://{issue}/{slot}@{v_out}", meter.usage)
                if v_esc > base_esc:
                    return AgentOutcome.escalation(
                        f"blackboard://{issue}/escalation.yaml@{v_esc}", meter.usage
                    )
                alt = self._alt_written(issue, base_alt)
                if alt is not None:
                    return AgentOutcome.artifact(alt, meter.usage)
                if nudged or resp.stop is StopReason.MAX_TOKENS:
                    return AgentOutcome.blocked(f"agent kết thúc mà không ghi {slot}", meter.usage)
                messages.append(Message.user_text(NUDGE_MSG.format(slot=slot)))
                nudged = True
                if tw is not None:
                    tw.write("harness", turn=meter.turns, text=NUDGE_MSG.format(slot=slot))
                continue

            # ── tool calls ──
            blocks: list[ToolResultBlock] = []
            written_uri: str | None = None
            escalated_uri: str | None = None
            stuck_stop: str | None = None
            for tc in calls:
                result = self._run_tool(tc.name, tc.input, tc.id, spec, ctx, tool_ctx, pctx)
                st = result.structured.get("stuck")
                if isinstance(st, dict) and st.get("stop"):
                    stuck_stop = str(st.get("hint") or "")
                if tw is not None:
                    tw.write(
                        "tool_result",
                        turn=meter.turns,
                        tool_use_id=tc.id,
                        name=tc.name,
                        status=result.status,
                        summary=result.summary,
                        error_code=result.error.code if result.error else None,
                        is_error=result.status != "ok",
                        content=result.to_model_text(),
                    )
                blocks.append(
                    ToolResultBlock(
                        tool_use_id=tc.id, content=result.to_model_text(), is_error=result.status != "ok"
                    )
                )
                if result.status == "ok" and result.structured.get("slot"):
                    s = str(result.structured["slot"])
                    v = int(result.structured.get("version", 0) or 0)
                    if s == slot and to_canonical(tc.name) == "blackboard.write":
                        written_uri = f"blackboard://{issue}/{slot}@{v}"
                    elif s == "escalation.yaml" and to_canonical(tc.name) == "blackboard.write":
                        escalated_uri = f"blackboard://{issue}/escalation.yaml@{v}"
            messages.append(Message(role="user", blocks=tuple(blocks)))
            if not caps.append_only_history and resp.usage.tokens_in >= condense_at:  # condenser tầng 2
                condensed, n = condense_tool_results(messages, keep_last_turns=spec.condenser.keep_last_turns)
                if n:
                    messages[:] = condensed
                    self._emit(
                        ctx,
                        EventType.CONTEXT_CONDENSED,
                        {"tool_results": n, "tokens_in": resp.usage.tokens_in},
                        budget=meter,
                    )
            if written_uri:
                return AgentOutcome.artifact(written_uri, meter.usage)
            if escalated_uri:
                return AgentOutcome.escalation(escalated_uri, meter.usage)
            if (
                stuck_stop is not None
            ):  # kẹt lần 2 trong lượt chạy → recovery lớp `stuck`, không đốt thêm budget
                return AgentOutcome.blocked(f"STUCK: {stuck_stop[:200]}", meter.usage)

    # ───── transport CLI (RunAgent) ─────
    def _run_cli_agent(
        self,
        spec: AgentSpec,
        brief: Brief,
        ctx: RunContext,
        client: LLMClient,
        provider: Any,
        tw: TranscriptWriter | None = None,
    ) -> AgentOutcome:
        """Loop chạy trong process CLI (`claude -p` / `codex exec`); tool qua MCP DDT (process `ddt mcp --session`).
        Harness vẫn: đếm usage/budget mỗi lượt (stream-json), kill khi chạm trần, hết hạn mức → fallback API cùng issue."""
        from digital_development_team.llm.cli_session import CliSession, TurnInfo

        issue, slot = ctx.issue_id, spec.output_slot
        system = compose_system_prompt(spec, self._agents, self._skills, self._resolver)
        brief_text = render_brief(brief, self._resolver)
        meter = BudgetMeter(ctx.budget, ctx.clock)
        base_out, base_esc = self._version(issue, slot), self._version(issue, "escalation.yaml")
        base_alt = {s: self._version(issue, s) for s in _alt_slots(spec)}
        seq0 = self._last_seq(issue)
        session = CliSession(
            home=str(self._home),
            repo=str(self._repo) if self._repo else None,
            config_dir=str(self._config_dir) if self._config_dir else None,
            issue_id=issue,
            role=spec.name,
            stage=ctx.stage.value,
            tier=ctx.tier.value,
            worktree=ctx.worktree,
            allowed_files=tuple(ctx.allowed_files),
            spec_values=self._spec_values(issue),
            autonomy=self._policy.autonomy,
            tools=tuple(spec.tool_names),
            preset=self._preset,
            flags=self._flags,
        )
        prompt_hash = sha256_text(system + brief_text)
        if tw is not None:
            tw.write(
                "system",
                role=spec.name,
                hash=sha256_text(system),
                chars=len(system),
                tools=list(spec.tool_names),
                model=client.ref.model,
                transport="cli",
            )
            tw.write("brief", text=brief_text)

        transport = client.ref.provider

        def on_turn(info: TurnInfo) -> bool:
            turn_cost = self._models.cost_of(client.ref, info.usage)
            meter.add_turn(
                info.usage.tokens_in,
                info.usage.tokens_out,
                cached=info.usage.tokens_cached,
                cost_usd=turn_cost,
            )
            if tw is not None:
                tw.write(
                    "assistant",
                    turn=meter.turns,
                    model=info.model or client.ref.model,
                    stop=info.stop_reason,
                    text=info.text,
                    tool_uses=[{"name": n} for n in info.tool_calls],
                    usage={
                        "tokens_in": info.usage.tokens_in,
                        "tokens_out": info.usage.tokens_out,
                        "tokens_cached": info.usage.tokens_cached,
                    },
                    transport="cli",
                )
            self._emit(
                ctx,
                EventType.LLM_CALL,
                {
                    "stop": info.stop_reason or "?",
                    "tool_calls": len(info.tool_calls),
                    "turn": meter.turns,
                    "fell_back": False,
                },
                llm=LlmInfo(
                    model=info.model or client.ref.model,
                    tokens_in=info.usage.tokens_in,
                    tokens_out=info.usage.tokens_out,
                    tokens_cached=info.usage.tokens_cached,
                    latency_ms=0,
                    cost_usd=turn_cost,
                    prompt_hash=prompt_hash,
                    transport="cli",
                ),
                budget=meter,
            )
            for lv in meter.crossed():
                self._emit(ctx, EventType.BUDGET_WARNING, {"level": lv, "scope": "stage"}, budget=meter)
            return _cli_may_continue(meter, info.tool_calls)  # False → provider kill process

        try:
            res = provider.run_agent(
                session=session,
                system=system,
                brief=brief_text,
                model=client.ref.model,
                effort=client.ref.effort,
                cancel=ctx.cancel,
                on_turn=on_turn,
                timeout_s=ctx.budget.wall_s + 60,
            )
        except ProviderError as e:
            if e.retryable and client.resolution.fallback is not None:
                return self._fallback_to_api(spec, brief, ctx, client, e.message, tw)
            if e.retryable:
                raise ProviderPaused(
                    f"{transport} tạm thời lỗi: {e.message}",
                    hint="chạy lại `ddt run <issue>` khi provider hồi phục (issue giữ nguyên stage)",
                ) from e
            self._emit(ctx, EventType.BLOCKED, {"reason": f"provider: {e.message}"}, budget=meter)
            return AgentOutcome.blocked(f"{transport}: {e.message}", meter.usage)

        v_out, v_esc = self._version(issue, slot), self._version(issue, "escalation.yaml")
        if v_out > base_out:
            return AgentOutcome.artifact(f"blackboard://{issue}/{slot}@{v_out}", meter.usage)
        if v_esc > base_esc:
            return AgentOutcome.escalation(f"blackboard://{issue}/escalation.yaml@{v_esc}", meter.usage)
        alt = self._alt_written(issue, base_alt)
        if alt is not None:
            return AgentOutcome.artifact(alt, meter.usage)
        axis = meter.exceeded_axis()
        if res.killed and axis is not None:
            self._emit(ctx, EventType.CIRCUIT_BREAKER, {"axis": axis, "scope": "stage"}, budget=meter)
            return AgentOutcome.blocked(f"hết budget stage ({axis}) — CLI bị dừng", meter.usage)
        if res.rate_limited:
            fb = client.resolution.fallback
            if fb is not None and not self._models.provider(fb).caps.agent_loop:
                self._models.mark_cooldown(client.ref.provider)
                return self._fallback_to_api(spec, brief, ctx, client, "CLI hết hạn mức subscription", tw)
            raise ProviderPaused(
                f"{transport} hết hạn mức, không có fallback API",
                hint="chạy lại `ddt run <issue>` khi provider hồi phục (issue giữ nguyên stage)",
            )
        reason = blocked_reason(extract_json_object(res.final_text), res.final_text)
        if reason:
            return AgentOutcome.blocked(reason, meter.usage)
        if res.is_error:
            return AgentOutcome.blocked(
                f"{transport} lỗi ({res.subtype}): {res.stderr_tail[-200:]}", meter.usage
            )
        if spec.output_mode is OutputMode.FINAL_MESSAGE:
            return self._write_final_cli(spec, issue, res.final_text, meter)
        if self._stuck_stopped(issue, ctx, since_seq=seq0):
            return AgentOutcome.blocked(
                f"STUCK: agent (CLI) dừng sau khi bị kẹt ({res.turns} lượt)", meter.usage
            )
        return AgentOutcome.blocked(
            f"agent (CLI) kết thúc sau {res.turns} lượt mà không ghi {slot}", meter.usage
        )

    def _fallback_to_api(
        self,
        spec: AgentSpec,
        brief: Brief,
        ctx: RunContext,
        client: LLMClient,
        why: str,
        tw: TranscriptWriter | None = None,
    ) -> AgentOutcome:
        fb = client.resolution.fallback
        assert fb is not None  # noqa: S101
        log.warning("%s: %s → fallback %s (API) trong cùng issue", spec.name, why, fb.alias)
        if tw is not None:
            tw.write("note", text=f"fallback từ CLI sang API ({fb.alias}): {why}")
        api_client = LLMClient(self._models, Resolution(spec.name, fb, None, (f"fallback từ CLI: {why}",)))
        return self._run_with_client(spec, brief, ctx, api_client, tw)

    # ───── nội bộ ─────
    def _last_seq(self, issue: str) -> int:
        from digital_development_team.eventlog.reader import read_events
        from digital_development_team.storage.layout import HomeLayout

        evs = read_events(HomeLayout(self._home), issue)
        return evs[-1].seq if evs else 0

    def _stuck_stopped(self, issue: str, ctx: RunContext, *, since_seq: int) -> bool:
        """MCP server (process khác) đã báo kẹt lần 2 trong lượt CLI này chưa."""
        from digital_development_team.eventlog.reader import read_events
        from digital_development_team.storage.layout import HomeLayout

        key = _actor_key(ctx)
        return any(
            e.event is EventType.STUCK_DETECTED
            and e.seq > since_seq
            and e.actor == key
            and e.data.get("stop")
            for e in read_events(HomeLayout(self._home), issue)
        )

    def _run_tool(
        self,
        wire: str,
        args: Mapping[str, Any],
        call_id: str,
        spec: AgentSpec,
        ctx: RunContext,
        tool_ctx: ToolContext,
        pctx: PolicyContext,
    ) -> ToolResult:
        try:
            name = to_canonical(wire)
        except ConfigError:
            return ToolResult.fail(
                "UNKNOWN_TOOL", f"tool {wire!r} không tồn tại", hint="Chỉ dùng tool trong danh sách"
            )
        call = ToolCall(
            id=call_id, name=name, args=dict(args), actor=ctx.actor, issue_id=ctx.issue_id, stage=ctx.stage
        )
        return self._dispatcher.dispatch(call, spec, tool_ctx, pctx)

    def _write_final(
        self,
        spec: AgentSpec,
        issue: str,
        obj: dict[str, Any] | None,
        messages: list[Message],
        attempt: int,
        *,
        tw: TranscriptWriter | None = None,
    ) -> str | None:
        """final_message: ghi artifact. Trả uri | '' (bỏ cuộc) | None (thử lại, đã append hướng dẫn)."""
        if obj is None:
            if attempt >= MAX_FINAL_ATTEMPTS:
                return ""
            messages.append(Message.user_text(NOT_JSON_MSG))
            if tw is not None:
                tw.write("harness", turn=attempt, text=NOT_JSON_MSG)
            return None
        try:
            ref = self._store.write(issue, spec.output_slot, obj, actor=messages_actor(spec))
            return ref.uri
        except SchemaError as e:
            if attempt >= MAX_FINAL_ATTEMPTS:
                return ""
            errors = "\n".join(f"- {v['path']}: {v['msg']}" for v in e.brief(5))
            retry_msg = SCHEMA_RETRY_MSG.format(n=attempt, max=MAX_FINAL_ATTEMPTS, errors=errors)
            messages.append(Message.user_text(retry_msg))
            if tw is not None:
                tw.write("harness", turn=attempt, text=retry_msg)
            return None
        except (SlotPermissionError, StoreError) as e:
            log.error("final write %s: %s", spec.output_slot, e)
            return ""

    def _write_final_cli(self, spec: AgentSpec, issue: str, text: str, meter: BudgetMeter) -> AgentOutcome:
        """final_message qua agent loop CLI: process đã kết thúc nên không có lượt sửa schema — coerce trong
        FileStore.write lo lỗi định dạng; còn sai thì blocked kèm lỗi cụ thể."""
        slot = spec.output_slot
        obj = extract_json_object(text)
        if obj is None:
            return AgentOutcome.blocked(
                f"agent (CLI) kết thúc mà lượt cuối không phải JSON cho {slot}", meter.usage
            )
        try:
            ref = self._store.write(issue, slot, obj, actor=messages_actor(spec))
        except SchemaError as e:
            errors = "; ".join(f"{v['path']}: {v['msg']}" for v in e.brief(3))
            return AgentOutcome.blocked(f"{slot} sai schema: {errors}", meter.usage)
        except (SlotPermissionError, StoreError) as e:
            log.error("final write %s: %s", slot, e)
            return AgentOutcome.blocked(f"ghi {slot} lỗi: {e.message}", meter.usage)
        return AgentOutcome.artifact(ref.uri, meter.usage)

    def _alt_written(self, issue: str, base: Mapping[str, int]) -> str | None:
        """Slot thay thế trong writes[] (vd Dev ghi answer.yaml khi kết luận không cần sửa) vừa có version mới."""
        for s, v0 in base.items():
            v = self._version(issue, s)
            if v > v0:
                return f"blackboard://{issue}/{s}@{v}"
        return None

    def _version(self, issue: str, slot: str) -> int:
        ref = self._store.latest(issue, slot)
        return ref.version if ref else 0

    def _spec_values(self, issue: str) -> dict[str, str | None]:
        try:
            sp = self._store.read(issue, "spec.yaml").content
        except StoreError:
            return {}
        return {
            "spec.test_command": sp.get("test_command"),
            "spec.regression_command": sp.get("regression_command"),
        }

    def _emit(
        self,
        ctx: RunContext,
        event: EventType,
        data: dict[str, Any],
        *,
        budget: BudgetMeter,
        llm: LlmInfo | None = None,
    ) -> None:
        actor = f"{ctx.actor.role}:{ctx.actor.sub_id}" if ctx.actor.sub_id else ctx.actor.role
        self._sink.emit(
            EventDraft(
                issue_id=ctx.issue_id,
                actor=actor,
                event=event,
                stage=ctx.stage,
                data=data,
                llm=llm,
                budget=BudgetSnapshot(**budget.snapshot()),
            )
        )


_MEMORY_READ = frozenset({"memory.search", "memory.get", "memory.worker_stats"})


def _actor_key(ctx: RunContext) -> str:
    return f"{ctx.actor.role}:{ctx.actor.sub_id}" if ctx.actor.sub_id else ctx.actor.role


def _alt_slots(spec: AgentSpec) -> list[str]:
    return [s for s in spec.writes[1:] if s not in ("escalation.yaml", "clarify.yaml")]


_MODEL_UNAVAILABLE = ("không nhận model", "unrecognized_model", "model not found", "model_not_found")


def _model_unavailable(reason: str | None) -> bool:
    low = (reason or "").lower()
    return any(k in low for k in _MODEL_UNAVAILABLE)


def _cli_may_continue(meter: BudgetMeter, tool_calls: tuple[str, ...]) -> bool:
    """Trần stage chạm ở lượt mà agent đang KẾT THÚC (không tool call = câu trả lời cuối; chỉ blackboard_write = ghi
    output) → để process chạy xong, giống lượt FINAL của loop API. Kill ở đây sẽ vứt đúng lượt có kết quả.
    Agent còn gọi tool làm việc khi đã chạm trần → kill. Ghi output tối đa MAX_FINAL_ATTEMPTS lượt quá trần (sửa schema)."""
    if meter.exceeded_axis() is None:
        return True
    finishing = all(n.endswith("blackboard_write") for n in tool_calls)  # rỗng → True
    return finishing and meter.turns <= meter.ref.turns + MAX_FINAL_ATTEMPTS


def messages_actor(spec: AgentSpec):  # type: ignore[no-untyped-def]
    from digital_development_team.core.context import Actor

    return Actor(role=spec.name)

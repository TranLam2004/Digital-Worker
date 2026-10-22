"""`ddt debug …` — thao tác trực tiếp blackboard/index để kiểm tay. Không dùng trong vận hành."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.cli.options import GlobalOpts
from digital_development_team.core.context import Actor
from digital_development_team.core.errors import DDTError, SchemaError
from digital_development_team.util import jsonio, yamlio
from digital_development_team.util.fsio import ENCODING

app = typer.Typer(no_args_is_help=True)
console = Console()


def _rt(ctx: typer.Context) -> Runtime:
    opts: GlobalOpts = ctx.obj
    return build_runtime(
        opts.resolved_home(),
        config_dir=opts.config_dir,
        flags=opts.flags,
        provider=opts.provider,
        repo=opts.repo,
    )


def _fail(e: DDTError) -> None:
    console.print(f"[red]✗ {e}[/red]")
    if isinstance(e, SchemaError):
        for v in e.brief():
            console.print(f"  [red]- {v['path']}: {v['msg']}[/red]")
    if e.hint:
        console.print(f"  [dim]→ {e.hint}[/dim]")
    raise typer.Exit(code=2)


@app.command("write-slot")
def write_slot(
    ctx: typer.Context,
    issue: str,
    slot: str,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    as_role: Annotated[
        str, typer.Option("--as", help="actor role: intake · dev · runner · human:P1")
    ] = "human",
) -> None:
    """Validate + ghi 1 artifact từ file yaml/json vào slot (đi qua đúng đường write của agent)."""
    rt = _rt(ctx)
    text = file.read_text(encoding=ENCODING)
    data = yamlio.loads(text, source=str(file)) if file.suffix in (".yaml", ".yml") else jsonio.loads(text)
    role, _, sub = as_role.partition(":")
    try:
        ref = rt.store.write(issue, slot, data, actor=Actor(role=role, sub_id=sub or None))
    except DDTError as e:
        _fail(e)
        return
    console.print(f"[green]✓ {ref.uri}[/green] → {ref.path} sha:{ref.sha256[:8]}")


@app.command("read-slot")
def read_slot(
    ctx: typer.Context,
    issue: str,
    slot: str,
    version: Annotated[int | None, typer.Option("--version", "-V")] = None,
    as_role: Annotated[str | None, typer.Option("--as", help="Đọc với ACL + projection của role")] = None,
) -> None:
    """In artifact (mới nhất hoặc version chỉ định)."""
    rt = _rt(ctx)
    try:
        if as_role:
            role, _, sub = as_role.partition(":")
            art = rt.store.read_as(issue, slot, Actor(role=role, sub_id=sub or None), version=version)
        else:
            art = rt.store.read(issue, slot, version=version)
    except DDTError as e:
        _fail(e)
        return
    console.print(f"[dim]{art.ref.uri} · {art.ref.written_by} · {art.ref.written_at.isoformat()}[/dim]")
    typer.echo(yamlio.dumps(art.content) if slot.endswith(".yaml") else jsonio.dumps(art.content))


@app.command("list-slots")
def list_slots(ctx: typer.Context, issue: str) -> None:
    """Index slot của một issue."""
    rt = _rt(ctx)
    t = Table("slot", "ver", "written_by", "written_at", "sha", "path")
    for r in rt.store.list(issue):
        t.add_row(r.slot, str(r.version), r.written_by, r.written_at.isoformat()[:19], r.sha256[:8], r.path)
    console.print(t)


@app.command("reindex")
def reindex_(ctx: typer.Context) -> None:
    """Dựng lại index SQLite từ work/** (fs là sự thật)."""
    from digital_development_team.blackboard.reindex import reindex

    rt = _rt(ctx)
    rep = reindex(rt.layout, rt.db, rt.registry)
    console.print(f"[green]✓ {rep.summary()}[/green]")
    for s in rep.schema_fail:
        console.print(f"  [yellow]schema: {s}[/yellow]")
    for s in rep.checkpoint_bad:
        console.print(f"  [red]checkpoint: {s}[/red]")


@app.command("emit")
def emit(
    ctx: typer.Context,
    issue: str,
    event: str,
    data: Annotated[str | None, typer.Option("--data", help='JSON, vd {"x":1}')] = None,
) -> None:
    """Ghi 1 event tay (kiểm writer + schema). Chỉ dùng khi KHÔNG có runner đang chạy cùng home."""
    from digital_development_team.core.enums import EventType
    from digital_development_team.core.events import EventDraft

    rt = _rt(ctx)
    try:
        ev = rt.sink.emit(
            EventDraft(
                issue_id=issue, actor="human", event=EventType(event), data=jsonio.loads(data) if data else {}
            )
        )
    except (DDTError, ValueError) as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=2) from None
    typer.echo(ev.to_line().rstrip())


@app.command("llm-ping")
def llm_ping(
    ctx: typer.Context,
    role: Annotated[str, typer.Option("--role", help="role trong models.yaml#roles")] = "llm_ask",
    prompt: Annotated[str, typer.Option("--prompt")] = "Trả lời đúng một từ: pong",
) -> None:
    """Gọi 1 lượt LLM theo role (resolve alias → provider thật hoặc --provider mock). In model, token, cost."""
    from digital_development_team.core.context import CancelToken
    from digital_development_team.core.llm import ChatRequest, Message

    rt = _rt(ctx)
    try:
        client = rt.models.client(role)
        req = ChatRequest(
            model=client.ref,
            system="Bạn là bộ kiểm tra kết nối.",
            messages=(Message.user_text(prompt),),
            max_tokens=32,
            metadata={"role": role, "issue": "ping"},
        )
        out = client.chat(req, cancel=CancelToken().child(timeout_s=60))
    except DDTError as e:
        _fail(e)
        return
    console.print(
        f"[green]✓ {out.ref.provider}/{out.ref.model}[/green] alias={out.ref.alias} stop={out.response.stop.value} "
        f"in={out.response.usage.tokens_in} out={out.response.usage.tokens_out} cached={out.response.usage.tokens_cached} "
        f"{out.response.usage.latency_ms}ms ${out.cost_usd:.5f} attempts={out.attempts}"
        + (f" retries={list(out.retries)}" if out.retries else "")
    )
    typer.echo(out.response.text)


def _cg_ctx(ctx: typer.Context):  # type: ignore[no-untyped-def]
    from digital_development_team.sandbox import build_sandbox

    rt = _rt(ctx)
    if rt.repo is None:
        console.print("[red]cần --repo[/red]")
        raise typer.Exit(code=2)
    sb = build_sandbox(rt.config.policy.sandbox, scratch=rt.layout.state_dir / "tmp")
    return rt, sb


@app.command("codegraph-init")
def codegraph_init(ctx: typer.Context) -> None:
    """Dựng index CodeGraph cho repo đích (<repo>/.codegraph/, exclude khỏi git). Chạy 1 lần lúc onboarding."""
    from digital_development_team.core.context import CancelToken
    from digital_development_team.tools.codegraph import ensure_index

    rt, sb = _cg_ctx(ctx)
    r = ensure_index(sb, rt.config.policy.sandbox, rt.repo, CancelToken())
    extra = {k: v for k, v in r.items() if k not in ("status", "summary")}
    console.print(
        f"[{'green' if r['status'] == 'ok' else 'yellow'}]{r['status']}[/] {r['summary']} {extra or ''}"
    )
    raise typer.Exit(code=0 if r["status"] == "ok" else 1)


@app.command("codegraph-status")
def codegraph_status(ctx: typer.Context) -> None:
    """Index CodeGraph: node/file, pending changes (stale), ngôn ngữ."""
    from digital_development_team.core.context import CancelToken
    from digital_development_team.tools.codegraph import status_of

    rt, sb = _cg_ctx(ctx)
    console.print(status_of(sb, rt.config.policy.sandbox, rt.repo, CancelToken()))


@app.command("codegraph-sync")
def codegraph_sync(ctx: typer.Context) -> None:
    """`codegraph sync` — cập nhật index sau khi repo đổi (runner tự gọi sau MERGE)."""
    from digital_development_team.core.context import CancelToken
    from digital_development_team.tools.codegraph import sync_index

    rt, sb = _cg_ctx(ctx)
    console.print(sync_index(sb, rt.config.policy.sandbox, rt.repo, CancelToken()))


@app.command("skills")
def skills_(ctx: typer.Context) -> None:
    """Bảng skill từ _assets/skills (+ override): source · used_by · trigger · token/cap · origin."""
    from digital_development_team.agents import SkillRegistry
    from digital_development_team.agents.skills import MAX_TOKENS

    rt = _rt(ctx)
    reg = SkillRegistry(rt.resolver)
    t = Table("skill", "source", "used_by", "trigger", "token/cap", "origin")
    for name in reg.names():
        s = reg.get(name)
        cap = MAX_TOKENS.get(s.meta.source, 1000)
        colour = "red" if s.tokens > cap else "green"
        t.add_row(
            s.name,
            s.meta.source,
            ", ".join(s.meta.used_by) or "—",
            s.meta.trigger,
            f"[{colour}]{s.tokens}[/{colour}]/{cap}",
            f"{s.meta.origin or '—'}" + (f" ({s.meta.license})" if s.meta.license else ""),
        )
    console.print(t)


@app.command("footprint")
def footprint_(ctx: typer.Context) -> None:
    """Token cố định mỗi lượt gọi model theo vai (system prompt + schema tool) — nhân với số lượt = credit Codex.
    Dùng với --match để thấy đúng cấu hình thi đấu (tool không ra điểm đã gỡ)."""
    from digital_development_team.agents import AgentRegistry, SkillRegistry
    from digital_development_team.agents.footprint import footprint
    from digital_development_team.app import build_tooling, mcp_grants

    rt = _rt(ctx)
    try:
        tooling = build_tooling(rt, base_ref=lambda _t: "main")
        reg = AgentRegistry(rt.resolver)
        reg.grant(mcp_grants(rt))
        reg.revoke(rt.config.policy.tool_denylist)
        t = Table("agent", "system", "tools (n)", "per call", "skills always")
        for f in footprint(reg, SkillRegistry(rt.resolver), rt.resolver, tooling.catalog):
            t.add_row(
                f.role, str(f.system), f"{f.tools} ({f.n_tools})", str(f.per_call), str(f.skills_always)
            )
        console.print(t)
        console.print("[dim]≈ ký tự/4; brief theo issue + lịch sử hội thoại cộng thêm mỗi lượt[/dim]")
    finally:
        rt.close()


@app.command("agents")
def agents_(ctx: typer.Context) -> None:
    """Bảng agent từ _assets/agents (+ override): stage · model · output_mode · tools · reads/writes."""
    from digital_development_team.agents import AgentRegistry

    rt = _rt(ctx)
    reg = AgentRegistry(rt.resolver)
    t = Table("agent", "stage", "model", "mode", "perm", "tools", "reads → writes")
    for s in reg:
        t.add_row(
            s.name,
            "/".join(s.stages),
            f"{s.model}→{s.fallback_model or '-'}",
            s.output_mode.value,
            s.permission_mode.value,
            str(len(s.tools)),
            f"{', '.join(s.reads) or '—'} → {', '.join(s.writes)}",
        )
    console.print(t)


@app.command("brief")
def brief_(
    ctx: typer.Context,
    issue: str,
    role: Annotated[str, typer.Option("--role", help="agent name: intake · planner · analyst · dev …")],
    rework: Annotated[int, typer.Option("--rework")] = 0,
    system: Annotated[bool, typer.Option("--system", help="In cả system prompt")] = False,
) -> None:
    """Sinh brief 4 khối cho agent từ artifact hiện có của issue (không gọi LLM)."""
    from digital_development_team.agents import (
        AgentRegistry,
        BriefBuilder,
        FsRepoContext,
        SkillRegistry,
        compose_system_prompt,
        render_brief,
    )
    from digital_development_team.agents.brief import BriefInputs
    from digital_development_team.budget import resolve_stage_budget
    from digital_development_team.core.context import BudgetRef
    from digital_development_team.core.enums import Tier

    opts: GlobalOpts = ctx.obj
    rt = _rt(ctx)
    try:
        agents = AgentRegistry(rt.resolver)
        skills = SkillRegistry(rt.resolver)
        spec = agents.get(role)
        cp = rt.checkpoints.load(issue)
        tier = cp.tier if cp else Tier.T1
        facts = dict(cp.facts) if cp else {}
        stage = spec.primary_stage or (cp.stage if cp else None)
        if stage is None:
            raise DDTError("agent async không có stage để sinh brief")
        budget = resolve_stage_budget(rt.config.budgets, stage, tier, rework=rework) or BudgetRef(
            turns=1, wall_s=60, tokens=4000
        )
        repo = FsRepoContext(opts.repo.resolve()) if opts.repo and opts.repo.is_dir() else None
        builder = BriefBuilder(
            store=rt.store,
            schemas=rt.registry,
            skills=skills,
            resolver=rt.resolver,
            policy=rt.config.policy,
            sensitive_globs=list(rt.config.sensitive_paths.globs()),
            repo=repo,
        )
        brief = builder.build(
            spec,
            BriefInputs(issue_id=issue, stage=stage, tier=tier, budget=budget, rework=rework, facts=facts),
        )
        if system:
            typer.echo("=" * 30 + " SYSTEM " + "=" * 30)
            typer.echo(compose_system_prompt(spec, agents, skills, rt.resolver))
            typer.echo("=" * 30 + " BRIEF " + "=" * 31)
        typer.echo(render_brief(brief, rt.resolver))
        tr = builder.last_trace
        console.print(f"[dim]~{tr.tokens} token · sections: {', '.join(tr.sections)}[/dim]")
        if tr.skipped:
            console.print(f"[dim]bỏ qua: {'; '.join(tr.skipped)}[/dim]")
        if tr.skills:
            console.print(f"[dim]skills: {'; '.join(tr.skills)}[/dim]")
    except DDTError as e:
        _fail(e)
    finally:
        rt.close()


@app.command("tool")
def tool_(
    ctx: typer.Context,
    issue: str,
    name: Annotated[str, typer.Argument(help="canonical: fs.read · exec.run · test.run · blackboard.read …")],
    role: Annotated[str, typer.Option("--role")] = "dev",
    args: Annotated[str, typer.Option("--args", help="JSON")] = "{}",
    worktree: Annotated[Path | None, typer.Option("--worktree", help="Mặc định = --repo")] = None,
) -> None:
    """Gọi 1 tool qua đúng đường policy → run → after → emit (như agent gọi). Cần --repo cho fs/git/exec/test."""
    from digital_development_team.agents import AgentRegistry
    from digital_development_team.app import build_tooling
    from digital_development_team.core.context import CancelToken, ToolContext
    from digital_development_team.core.enums import Tier
    from digital_development_team.core.models import ToolCall
    from digital_development_team.policy.engine import PolicyContext

    opts: GlobalOpts = ctx.obj
    rt = _rt(ctx)
    try:
        spec = AgentRegistry(rt.resolver).get(role)
        tooling = build_tooling(rt)
        wt = worktree or opts.repo
        wt_s = str(wt.resolve()) if wt else None
        cp = rt.checkpoints.load(issue)
        allowed: tuple[str, ...] = ()
        spec_vals: dict[str, str | None] = {}
        try:
            sp = rt.store.read(issue, "spec.yaml").content
            allowed = tuple(str(x) for x in sp.get("allowed_files", []))
            spec_vals = {
                "spec.test_command": sp.get("test_command"),
                "spec.regression_command": sp.get("regression_command"),
            }
        except DDTError:
            pass
        stage = spec.primary_stage or (cp.stage if cp else None)
        if stage is None:
            raise DDTError("không xác định được stage")
        actor = Actor(role=role)
        call = ToolCall(
            id="debug", name=name, args=jsonio.loads(args), actor=actor, issue_id=issue, stage=stage
        )
        tctx = ToolContext(
            issue_id=issue,
            actor=actor,
            stage=stage,
            worktree=wt_s,
            home=str(rt.home),
            allowed_files=allowed,
            cancel=CancelToken(),
            clock=rt.clock,
        )
        pctx = PolicyContext(
            tier=cp.tier if cp else Tier.T1,
            worktree=wt_s,
            allowed_files=allowed,
            spec_values=spec_vals,
            autonomy=rt.config.policy.autonomy,
        )
        res = tooling.dispatcher.dispatch(call, spec, tctx, pctx)
    except DDTError as e:
        _fail(e)
        return
    finally:
        rt.close()
    colour = {"ok": "green", "error": "yellow", "denied": "red"}[res.status]
    console.print(f"[{colour}]{res.status}[/{colour}] {res.summary}")
    typer.echo(res.to_model_text())

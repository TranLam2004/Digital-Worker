"""`ddt run · stop · resume · gates · resolve` — vận hành một issue và kiểm soát của người."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.app import Runtime, build_runtime
from digital_development_team.cli.options import GlobalOpts
from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import GateId, Stage
from digital_development_team.core.errors import DDTError
from digital_development_team.gates.service import GateService
from digital_development_team.gates.stop import StopSwitch
from digital_development_team.ingest.queue import Queue
from digital_development_team.storage.layout import HomeLayout

console = Console()
app = typer.Typer()


def _rt(ctx: typer.Context, golden: Path | None = None) -> Runtime:
    opts: GlobalOpts = ctx.obj
    try:
        return build_runtime(
            opts.resolved_home(),
            config_dir=opts.config_dir,
            flags=opts.flags,
            provider=opts.provider,
            golden_dir=golden,
            repo=opts.repo,
        )
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
        raise typer.Exit(code=2) from None


@app.command("run")
def run(
    ctx: typer.Context,
    issue: Annotated[str | None, typer.Argument(help="DDT-0001 (bỏ trống với --next)")] = None,
    until: Annotated[Stage | None, typer.Option("--until", help="Dừng sau khi stage này xong")] = None,
    stub_stages: Annotated[
        bool, typer.Option("--stub-stages", help="Mọi stage agent = stub (0 LLM)")
    ] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Chờ gate thay vì thoát mã 4")] = False,
    golden: Annotated[
        Path | None, typer.Option("--golden", help="Thư mục golden cho --provider mock")
    ] = None,
    next_: Annotated[bool, typer.Option("--next", help="Lấy issue kế tiếp từ queue")] = False,
) -> None:
    """Chạy một issue tới DONE/BLOCKED (0) · paused (3) · chờ gate (4). Resume tự động từ checkpoint."""
    from digital_development_team.runner import Runner, default_handlers

    rt = _rt(ctx, golden)
    if issue is None:
        if not next_:
            rt.close()
            console.print("[red]cần ISSUE hoặc --next[/red]")
            raise typer.Exit(code=2)
        row = Queue(rt.db, rt.clock).next()
        if row is None:
            rt.close()
            console.print("[dim]hàng đợi trống hoặc mọi issue đang chờ blocked_by[/dim]")
            raise typer.Exit(code=0)
        issue = row.issue_id
    runner = Runner(
        rt.services(with_agents=not stub_stages),
        handlers=default_handlers(stub=stub_stages),
        stub=stub_stages,
    )
    cancel = CancelToken()
    try:
        rep = runner.run(issue, cancel=cancel, until=until, wait_gates=wait)
    except KeyboardInterrupt:
        cancel.cancel("ctrl-c")
        console.print("[yellow]⏸ ctrl-c — checkpoint đã lưu, `ddt run` lại để tiếp[/yellow]")
        raise typer.Exit(code=3) from None
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
        raise typer.Exit(code=2) from None
    finally:
        rt.close()
    colour = {
        "done": "green",
        "blocked": "red",
        "paused": "yellow",
        "waiting_gate": "cyan",
        "running": "blue",
    }[rep.status]
    console.print(
        f"[{colour}]{rep.issue_id} · {rep.status} @ {rep.stage.value}[/{colour}]"
        + (f" — {rep.reason}" if rep.reason else "")
        + f"  [dim]({rep.loops} vòng, {rep.events} event)[/dim]"
    )
    if rep.status == "waiting_gate":
        console.print(
            f"  [dim]→ ddt gates · ddt resolve {rep.issue_id} {rep.stage.value.split('_')[0]} --approve[/dim]"
        )
    raise typer.Exit(code=rep.exit_code)


@app.command("stop")
def stop(ctx: typer.Context, by: Annotated[str, typer.Option("--by")] = "human") -> None:
    """G3 — tạo state/STOP. Runner đang chạy hoàn thành tool call hiện tại, checkpoint, dừng."""
    opts: GlobalOpts = ctx.obj
    sw = StopSwitch(HomeLayout(opts.resolved_home()).ensure())
    sw.request(by=by)
    console.print(f"[red]■ STOP[/red] ({sw.info()}) — `ddt resume` để tiếp")


@app.command("resume")
def resume(ctx: typer.Context, issue: Annotated[str | None, typer.Argument()] = None) -> None:
    """Xoá STOP; nếu có ISSUE thì chạy tiếp issue đó (tương đương `ddt run`)."""
    opts: GlobalOpts = ctx.obj
    sw = StopSwitch(HomeLayout(opts.resolved_home()).ensure())
    console.print("[green]▶ resume[/green]" if sw.clear() else "[dim]không có STOP[/dim]")
    if issue:
        ctx.invoke(
            run, ctx=ctx, issue=issue, until=None, stub_stages=False, wait=False, golden=None, next_=False
        )


@app.command("gates")
def gates(ctx: typer.Context, issue: Annotated[str | None, typer.Argument()] = None) -> None:
    """Gate đang chờ người (G0 scope · G1 merge)."""
    rt = _rt(ctx)
    try:
        rows = GateService(rt.db, rt.clock).list_open(issue)
    finally:
        rt.close()
    if not rows:
        console.print("[dim]không có gate đang mở[/dim]")
        return
    t = Table("ticket", "issue", "gate", "tier", "mở lúc", "timeout", "tóm tắt")
    for g in rows:
        summ = g.summary
        brief_raw = summ.get("decision") or summ.get("intake") or {}
        brief: dict[str, object] = brief_raw if isinstance(brief_raw, dict) else {}
        t.add_row(
            g.id,
            g.ticket.issue_id,
            g.ticket.gate.value,
            g.tier,
            g.ticket.opened_at.strftime("%H:%M:%S"),
            "∞" if g.ticket.timeout_s is None else f"{g.ticket.timeout_s:g}s",
            " · ".join(f"{k}={v}" for k, v in brief.items() if isinstance(v, (str, int, float)))[:80],
        )
    console.print(t)


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    issue: str,
    gate: Annotated[GateId, typer.Argument(help="G0 | G1")],
    approve: Annotated[bool, typer.Option("--approve")] = False,
    reject: Annotated[bool, typer.Option("--reject", help="Rework với lý do (--note)")] = False,
    reject_final: Annotated[bool, typer.Option("--reject-final", help="BLOCKED")] = False,
    choice: Annotated[str | None, typer.Option("--choice", help="A | B … cho escalation")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    by: Annotated[str, typer.Option("--by")] = "human:P1",
) -> None:
    """Người quyết gate. Ghi vào gate_requests; worker/runner thấy ở lần poll kế (hoặc `ddt run` lại)."""
    from digital_development_team.gates.decisions import pick_choice, resolve_gate

    try:
        picked = pick_choice(approve=approve, reject=reject, reject_final=reject_final, choice=choice)
    except DDTError as e:
        console.print(f"[red]{e.message}[/red]")
        raise typer.Exit(code=2) from None
    rt = _rt(ctx)
    try:
        svc = GateService(rt.db, rt.clock)
        og = svc.open_for(issue, gate)
        if og is None:
            console.print(f"[red]không có gate {gate.value} đang mở cho {issue}[/red]")
            raise typer.Exit(code=2)
        options: list[str] | None = None
        if og.escalation_uri:
            try:
                form = rt.store.read(issue, "escalation.yaml").content
                options = [str(o.get("id")) for o in form.get("options", []) if isinstance(o, dict)]
            except DDTError:
                options = None
        resolve_gate(svc, og, choice=picked, by=by, note=note, clock=rt.clock, options=options)
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=2) from None
    finally:
        rt.close()
    console.print(f"[green]✓ {og.id} → {picked}[/green] by {by}")


@app.command("reopen")
def reopen(
    ctx: typer.Context,
    issue: str,
    option: Annotated[str | None, typer.Option("--option", help="A | B … trong blocked_report.json")] = None,
    at: Annotated[str | None, typer.Option("--at", help="Stage đích: LOCALIZE · SPEC · IMPLEMENT …")] = None,
    note: Annotated[str | None, typer.Option("--note", help="Chỉ dẫn cho agent")] = None,
    allow: Annotated[list[str] | None, typer.Option("--allow", help="Thêm file vào allowed_files")] = None,
    budget: Annotated[int, typer.Option("--budget", help="Cấp thêm % budget issue")] = 0,
    by: Annotated[str, typer.Option("--by")] = "human:P1",
) -> None:
    """Mở lại issue BLOCKED theo blocked_report.json (hoặc tự chỉ định). `ddt run` lần sau chạy tiếp từ worktree cũ."""
    from digital_development_team.runner.reopen import reopen_issue

    rt = _rt(ctx)
    try:
        ok, res = reopen_issue(
            rt.services(),
            rt.config.policy,
            issue,
            by=by,
            stage=at,
            option=option,
            note=note,
            allow=allow or [],
            budget_pct=budget,
        )
    finally:
        rt.close()
    if not ok:
        console.print(f"[red]✗ {res.get('error')}[/red]")
        raise typer.Exit(code=2)
    extra = f" · +{res['budget_pct']}% budget" if res.get("budget_pct") else ""
    files = f" · allowed += {', '.join(res['allowed_files_add'])}" if res.get("allowed_files_add") else ""
    console.print(f"[green]✓ {issue} mở lại ở {res['stage']}[/green] (lần {res['reopens']}){files}{extra}")

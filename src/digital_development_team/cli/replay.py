"""`ddt replay ISSUE [--until N]` — phát lại events (không chạy lại tool) · `ddt report` — 1 trang."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.eventlog.reader import read_events
from digital_development_team.eventlog.replay import replay as replay_lines
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout

console = Console()
app = typer.Typer()


@app.command("replay")
def replay(
    ctx: typer.Context,
    issue: str,
    until: Annotated[int | None, typer.Option("--until", help="Dừng ở seq N")] = None,
    resume: Annotated[bool, typer.Option("--resume", help="Dựng lại checkpoint từ events (TODO P2)")] = False,
) -> None:
    """Tua lại một issue từng event theo seq. Side effect đã xảy ra — chỉ xem lại."""
    opts: GlobalOpts = ctx.obj
    layout = HomeLayout(opts.resolved_home())
    if resume:
        console.print("[yellow]--resume chưa hỗ trợ (P2) — checkpoint.json vẫn là nguồn resume[/yellow]")
        raise typer.Exit(code=2)
    evs = read_events(layout, issue)
    if not evs:
        console.print(f"[dim]{issue}: chưa có events[/dim]")
        raise typer.Exit(code=1)
    for line in replay_lines(evs, until=until):
        typer.echo(line)
    db_path = layout.db_path
    if db_path.is_file():
        from digital_development_team.blackboard.checkpoints import CheckpointStore

        cp = CheckpointStore(layout, Database(db_path)).load(issue)
        if cp is not None:
            console.print(
                f"[dim]checkpoint: {cp.status.value} @ {cp.stage.value} · tier {cp.tier.value} · rework {cp.rework} · "
                f"{cp.usage.tokens} tok · last_seq {cp.last_seq}[/dim]"
            )


@app.command("report")
def report(
    ctx: typer.Context,
    write: Annotated[
        bool, typer.Option("--write", help="Ghi reports/session_<ts>.md trong DDT home")
    ] = False,
) -> None:
    """Báo cáo 1 trang: issue · tier · gate · chất lượng · chi phí · theo role."""
    from datetime import UTC, datetime

    from digital_development_team.reporting import build_report, render_report
    from digital_development_team.util.fsio import write_text_atomic

    opts: GlobalOpts = ctx.obj
    layout = HomeLayout(opts.resolved_home())
    if not layout.db_path.is_file():
        console.print("[dim]chưa có dữ liệu[/dim]")
        raise typer.Exit(code=1)
    text = render_report(build_report(layout, Database(layout.db_path)))
    typer.echo(text)
    if write:
        name = f"session_{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.md"
        write_text_atomic(layout.reports_dir / name, f"# DDT session report\n\n```\n{text}```\n", fsync=False)
        console.print(f"[green]✓ {layout.reports_dir / name}[/green]")


@app.command("handover")
def handover(
    ctx: typer.Context,
    issue: str,
    write: Annotated[bool, typer.Option("--write", help="Ghi reports/handover_<issue>.md")] = False,
) -> None:
    """Biên bản bàn giao issue (PR description): spec · AC + bằng chứng · security · thay đổi · ai duyệt."""
    from datetime import UTC, datetime

    from digital_development_team.blackboard.store import FileStore
    from digital_development_team.config.resources import build_resolver
    from digital_development_team.core.errors import DDTError
    from digital_development_team.eventlog.memory import InMemorySink
    from digital_development_team.reporting import build_handover
    from digital_development_team.schema.registry import SchemaRegistry
    from digital_development_team.util.fsio import write_text_atomic

    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home()
    layout = HomeLayout(home)
    if not layout.db_path.is_file():
        console.print("[dim]chưa có dữ liệu[/dim]")
        raise typer.Exit(code=1)
    db = Database(layout.db_path)
    store = FileStore(layout, db, SchemaRegistry(build_resolver(home=home)), InMemorySink())
    try:
        h = build_handover(layout, db, store, issue, now=datetime.now(UTC))
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=1) from None
    finally:
        db.close()
    typer.echo(h.markdown)
    if write:
        p = layout.reports_dir / f"handover_{issue}.md"
        write_text_atomic(p, h.markdown, fsync=False)
        console.print(f"[green]✓ {p}[/green]")


@app.command("perf")
def perf(
    ctx: typer.Context,
    write: Annotated[bool, typer.Option("--write", help="Ghi reports/perf_<N>.md")] = False,
    backfill: Annotated[
        bool,
        typer.Option(
            "--backfill",
            help="Ghi kết quả cho issue đã kết thúc trước khi có số liệu học hỏi (phiên bản JD ghi là 0, không rõ)",
        ),
    ] = False,
    every: Annotated[int, typer.Option("--every", help="Cỡ cửa sổ so sánh")] = 10,
) -> None:
    """Performance review từ role_outcomes: pass theo role, so sánh phiên bản JD, module khó, học hỏi. 0 LLM."""
    from datetime import UTC, datetime

    from digital_development_team.blackboard.checkpoints import CheckpointStore
    from digital_development_team.blackboard.store import FileStore
    from digital_development_team.config.resources import build_resolver
    from digital_development_team.eventlog.memory import InMemorySink
    from digital_development_team.memory.outcomes import backfill_outcomes
    from digital_development_team.reporting.perf import build_perf_review
    from digital_development_team.schema.registry import SchemaRegistry
    from digital_development_team.util.fsio import write_text_atomic

    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home()
    layout = HomeLayout(home)
    if not layout.db_path.is_file():
        console.print("[dim]chưa có dữ liệu[/dim]")
        raise typer.Exit(code=1)
    db = Database(layout.db_path)
    try:
        now = datetime.now(UTC)
        if backfill:
            store = FileStore(layout, db, SchemaRegistry(build_resolver(home=home)), InMemorySink())
            added = backfill_outcomes(
                db=db, layout=layout, store=store, checkpoints=CheckpointStore(layout, db)
            )
            console.print(f"[green]✓ backfill {added} issue[/green]")
        upto = int(db.scalar("SELECT COALESCE(MAX(seq_no), 0) FROM role_outcomes") or 0)
        if upto == 0:
            console.print("[dim]chưa có issue nào kết thúc có số liệu (thử --backfill)[/dim]")
            raise typer.Exit(code=1)
        text = build_perf_review(db, upto=upto, every=every, now=now)
        typer.echo(text)
        if write:
            p = layout.reports_dir / f"perf_{upto:03d}.md"
            write_text_atomic(p, text, fsync=False)
            console.print(f"[green]✓ {p}[/green]")
    finally:
        db.close()


@app.command("security-report")
def security_report_cmd(
    ctx: typer.Context,
    issue: str,
    write: Annotated[bool, typer.Option("--write", help="Ghi reports/security_<issue>.md")] = False,
) -> None:
    """Báo cáo sự cố bảo mật của một issue: advisory · 5C · finding · việc cần làm ngay · dòng thời gian."""
    from datetime import UTC, datetime

    from digital_development_team.blackboard.store import FileStore
    from digital_development_team.config.resources import build_resolver
    from digital_development_team.core.errors import DDTError
    from digital_development_team.eventlog.memory import InMemorySink
    from digital_development_team.reporting.security import build_security_report
    from digital_development_team.schema.registry import SchemaRegistry
    from digital_development_team.util.fsio import write_text_atomic

    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home()
    layout = HomeLayout(home)
    if not layout.db_path.is_file():
        console.print("[dim]chưa có dữ liệu[/dim]")
        raise typer.Exit(code=1)
    db = Database(layout.db_path)
    store = FileStore(layout, db, SchemaRegistry(build_resolver(home=home)), InMemorySink())
    try:
        r = build_security_report(layout, db, store, issue, now=datetime.now(UTC))
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=1) from None
    finally:
        db.close()
    typer.echo(r.markdown)
    if write:
        p = layout.reports_dir / f"security_{issue}.md"
        write_text_atomic(p, r.markdown, fsync=False)
        console.print(f"[green]✓ {p}[/green]")

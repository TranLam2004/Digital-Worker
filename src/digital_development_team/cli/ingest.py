"""`ddt ingest <file>` · `ddt queue` — backlog vào hàng đợi (0 LLM)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.app import build_runtime
from digital_development_team.cli.options import GlobalOpts
from digital_development_team.core.errors import DDTError
from digital_development_team.ingest import Queue, ingest_file

console = Console()
app = typer.Typer()


@app.command("ingest")
def ingest(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="backlog .csv | .json")],
    source: Annotated[
        str | None, typer.Option("--source", help="Tên nguồn (mặc định theo đuôi file)")
    ] = None,
) -> None:
    """Đọc backlog → item.json + queue. Trùng (source, id) → bỏ qua. Không gọi LLM."""
    opts: GlobalOpts = ctx.obj
    rt = build_runtime(
        opts.resolved_home(), config_dir=opts.config_dir, flags=opts.flags, provider=opts.provider
    )
    try:
        rep = ingest_file(
            file, store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock, source_name=source
        )
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
        raise typer.Exit(code=2) from None
    finally:
        rt.close()
    console.print(f"[green]✓ {rep.summary()}[/green]")
    for i in rep.created:
        console.print(f"  + {i}")
    for s in rep.skipped:
        console.print(f"  [dim]= {s} (đã có)[/dim]")
    for err in rep.errors:
        console.print(f"  [red]✗ {err}[/red]")
    if rep.errors:
        raise typer.Exit(code=1)


@app.command("queue")
def queue_(ctx: typer.Context, status: Annotated[str | None, typer.Option("--status")] = None) -> None:
    """Hàng đợi issue theo ưu tiên (P0 security > P0 > P1 > P2 > P3)."""
    opts: GlobalOpts = ctx.obj
    rt = build_runtime(
        opts.resolved_home(), config_dir=opts.config_dir, flags=opts.flags, provider=opts.provider
    )
    try:
        q = Queue(rt.db, rt.clock)
        rows = q.list(status)
        nxt = q.next()
    finally:
        rt.close()
    t = Table("issue", "prio", "status", "source", "title", "blocked_by", "enqueued")
    for r in rows:
        mark = " ◀ next" if nxt and r.issue_id == nxt.issue_id else ""
        t.add_row(
            r.issue_id + mark,
            str(r.priority),
            r.status,
            f"{r.source}:{r.source_id}",
            r.title[:60],
            ",".join(r.blocked_by),
            r.enqueued_at[:19],
        )
    console.print(t)

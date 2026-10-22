"""`ddt watch [ISSUE]` — tail events.jsonl ra terminal. `ddt board` — bảng issue."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.core.context import CancelToken
from digital_development_team.eventlog.reader import follow, read_events
from digital_development_team.eventlog.replay import format_event
from digital_development_team.storage.layout import GLOBAL_ISSUE, HomeLayout

console = Console()
app = typer.Typer()


@app.command("watch")
def watch(
    ctx: typer.Context,
    issue: Annotated[str, typer.Argument(help="DDT-0001 hoặc 'global'")] = GLOBAL_ISSUE,
    follow_: Annotated[
        bool, typer.Option("--follow/--no-follow", "-f", help="Theo dõi file (tail -f)")
    ] = True,
    since: Annotated[int, typer.Option("--since", help="Chỉ in seq > N")] = -1,
) -> None:
    """In events.jsonl của một issue, mặc định theo dõi liên tục (Ctrl+C dừng)."""
    opts: GlobalOpts = ctx.obj
    layout = HomeLayout(opts.resolved_home())
    path = layout.events_path(issue)
    if not follow_:
        for ev in read_events(layout, issue, since=since):
            typer.echo(format_event(ev))
        return
    console.print(f"[dim]watch {path} — Ctrl+C để dừng[/dim]")
    cancel = CancelToken()
    try:
        for ev in follow(path, cancel=cancel):
            if ev.seq > since:
                typer.echo(format_event(ev))
    except KeyboardInterrupt:
        cancel.cancel("ctrl-c")


@app.command("board")
def board(ctx: typer.Context) -> None:
    """Trạng thái mọi issue (từ bảng board — checkpoint gần nhất)."""
    from digital_development_team.blackboard.checkpoints import CheckpointStore
    from digital_development_team.storage.db import Database

    opts: GlobalOpts = ctx.obj
    layout = HomeLayout(opts.resolved_home())
    if not layout.db_path.is_file():
        console.print("[yellow]chưa có ddt.db — chưa chạy issue nào[/yellow]")
        return
    rows = CheckpointStore(layout, Database(layout.db_path)).board()
    t = Table("issue", "status", "stage", "tier", "rework", "attempt", "updated", "title")
    for r in rows:
        t.add_row(
            r.issue_id,
            r.status.value,
            r.stage,
            r.tier,
            str(r.rework),
            str(r.attempt),
            r.updated_at[:19],
            r.title or "",
        )
    console.print(t)

"""`ddt schema check|list|contract`."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.config.resources import build_resolver
from digital_development_team.core.errors import ConfigError
from digital_development_team.schema import SLOT_SCHEMA, SchemaRegistry, render_contract

app = typer.Typer(no_args_is_help=True)
console = Console()


def _registry(ctx: typer.Context) -> SchemaRegistry:
    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home() if (opts.repo or opts.home) else None
    return SchemaRegistry(build_resolver(home=home, config_dir=opts.config_dir))


@app.command("check")
def check(ctx: typer.Context) -> None:
    """Kiểm mọi schema hợp lệ draft-07, examples pass, slot map đủ."""
    try:
        reg = _registry(ctx)
    except ConfigError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=2) from None
    problems = reg.check_all()
    for sid in reg.ids():
        n_ex = len(reg.schema(sid).get("examples", []))
        console.print(f"  [dim]{sid:12} ({reg.source(sid)}) examples={n_ex}[/dim]")
    for sid, msg in problems:
        console.print(f"  [red]✗ {sid}: {msg}[/red]")
    if problems:
        console.print(f"[red]✗ {len(problems)} lỗi[/red]")
        raise typer.Exit(code=2)
    console.print(f"[green]✓ {len(reg.ids())} schema OK[/green]")


@app.command("list")
def list_(ctx: typer.Context) -> None:
    """Bảng slot → schema."""
    reg = _registry(ctx)
    t = Table("slot", "schema", "required", "source")
    for slot, sid in SLOT_SCHEMA.items():
        s = reg.schema(sid)
        t.add_row(slot, sid, ", ".join(s.get("required", [])), reg.source(sid))
    console.print(t)


@app.command("contract")
def contract(ctx: typer.Context, slot: Annotated[str, typer.Argument(help="VD spec.yaml")]) -> None:
    """In khối OUTPUT CONTRACT như agent sẽ thấy trong brief."""
    reg = _registry(ctx)
    s = reg.for_slot(slot)
    if s is None:
        console.print(f"[red]slot {slot!r} không có schema[/red]")
        raise typer.Exit(code=2)
    typer.echo(render_contract(s, slot))

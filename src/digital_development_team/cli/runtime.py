"""`ddt runtime show|set|unset|clear` — 3 cần điều khiển: patch dotted vào <home>/overrides/config/runtime.yaml
(worker hot-reload ở issue kế tiếp)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.console import Console

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.config.runtime_patch import (
    clear_runtime,
    read_runtime,
    set_runtime,
    unset_runtime,
)
from digital_development_team.core.errors import DDTError

console = Console()
runtime_app = typer.Typer(
    no_args_is_help=True, help="3 cần điều khiển: patch dotted vào overrides/config/runtime.yaml"
)


def _fail(e: DDTError) -> None:
    console.print(f"[red]✗ {e.message}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
    raise typer.Exit(code=2)


@runtime_app.command("show")
def runtime_show(ctx: typer.Context) -> None:
    """runtime.yaml hiện tại + config hiệu lực."""
    from digital_development_team.config.loader import load_config

    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home()
    console.print(json.dumps(read_runtime(home), ensure_ascii=False, indent=2) or "{}")
    try:
        cfg = load_config(home=home, config_dir=opts.config_dir)
        console.print(
            f"[dim]hiệu lực: autonomy={cfg.policy.autonomy} wip={cfg.policy.wip_limit} economy={cfg.policy.economy_mode} fp={cfg.fingerprint[:12]}[/dim]"
        )
    except DDTError as e:
        _fail(e)


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@runtime_app.command("set")
def runtime_set(
    ctx: typer.Context,
    assignment: Annotated[
        str,
        typer.Argument(
            help="policy.autonomy=aggressive · policy.wip_limit=2 · preset=economy · flags='[\"--no-canary\"]'"
        ),
    ],
) -> None:
    """Ghi một patch dotted. Validate kết quả; lỗi → hoàn tác."""
    from digital_development_team.config.loader import load_config

    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home()
    if "=" not in assignment:
        _fail(DDTError("cần dạng section.key=value"))
    key, _, raw = assignment.partition("=")
    value = _parse_value(raw)
    section, _, dotted = key.partition(".")
    before = read_runtime(home)
    try:
        set_runtime(home, section, dotted or None, value)
        cfg = load_config(home=home, config_dir=opts.config_dir)
    except DDTError as e:
        from digital_development_team.config.runtime_patch import write_runtime

        write_runtime(home, before)
        _fail(e)
    console.print(
        f"[green]✓ {key} = {value!r}[/green] → autonomy={cfg.policy.autonomy} wip={cfg.policy.wip_limit} (hiệu lực ở issue kế)"
    )


@runtime_app.command("unset")
def runtime_unset(ctx: typer.Context, key: str) -> None:
    opts: GlobalOpts = ctx.obj
    section, _, dotted = key.partition(".")
    unset_runtime(opts.resolved_home(), section, dotted or None)
    console.print(f"[green]✓ bỏ {key}[/green]")


@runtime_app.command("clear")
def runtime_clear(ctx: typer.Context) -> None:
    opts: GlobalOpts = ctx.obj
    console.print(
        "[green]✓ xoá runtime.yaml[/green]" if clear_runtime(opts.resolved_home()) else "[dim]không có[/dim]"
    )

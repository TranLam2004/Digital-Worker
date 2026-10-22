"""`ddt config check|show|sources`."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.config.checks import run_checks
from digital_development_team.config.loader import load_config
from digital_development_team.core.errors import ConfigError

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("profile")
def profile(
    ctx: typer.Context,
    mode: Annotated[str, typer.Argument(help="daily | contest")],
    fast_model: Annotated[str, typer.Option("--fast-model", help="Exact GPT model ID available in Codex")],
    main_model: Annotated[str, typer.Option("--main-model")],
    strong_model: Annotated[str, typer.Option("--strong-model")],
    budget_usd: Annotated[float, typer.Option("--budget-usd", min=0.01)],
    tokens: Annotated[int, typer.Option("--tokens", min=1)] = 1_000_000,
) -> None:
    """Create a separate home profile before work starts. Never overwrite an existing runtime file."""
    from digital_development_team.config.profiles import codex_profile
    from digital_development_team.config.runtime_patch import flatten, runtime_path, write_runtime

    opts: GlobalOpts = ctx.obj
    if opts.home is None:
        raise typer.BadParameter("Use --home explicitly; keep daily and each contest match in separate homes")
    if runtime_path(opts.home).exists():
        raise typer.BadParameter(
            "runtime.yaml already exists; choose a new home or edit it deliberately before Start"
        )
    try:
        data = codex_profile(
            mode,
            fast_model=fast_model,
            main_model=main_model,
            strong_model=strong_model,
            budget_usd=budget_usd,
            tokens=tokens,
        )
        load_config(
            config_dir=opts.config_dir,
            preset=data["preset"],
            flags=data["flags"],
            overrides=[
                (section, dotted, value)
                for section in ("models", "policy", "budgets")
                for dotted, value in flatten(data[section])
            ],
        )
        path = write_runtime(opts.home, data)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from None
    console.print(f"[green]Created {mode} profile:[/green] {path}")
    console.print(
        "USD is a local estimate; reconcile with organizer credits. Run model preflight before Start."
    )


def _load(ctx: typer.Context, preset: str | None, flags: list[str]) -> object:
    opts: GlobalOpts = ctx.obj
    if preset is None and opts.provider:  # --provider X → preset tương ứng (như build_runtime)
        from digital_development_team.app import PROVIDER_PRESETS

        preset = PROVIDER_PRESETS.get(opts.provider)
    return load_config(
        home=opts.resolved_home() if opts.repo or opts.home else None,
        config_dir=opts.config_dir,
        flags=[*opts.flags, *flags],  # cờ gốc (--match, --flag) + cờ riêng của lệnh
        preset=preset,
    )


def _check_skills(ctx: typer.Context, report: object) -> None:
    """Skill × agent (L6) — dựng registry từ cùng resolver; lỗi nạp skill = lỗi config."""
    from digital_development_team.agents import AgentRegistry, SkillRegistry
    from digital_development_team.agents.checks import check_skills
    from digital_development_team.config.resources import build_resolver

    opts: GlobalOpts = ctx.obj
    resolver = build_resolver(
        home=opts.resolved_home() if opts.repo or opts.home else None, config_dir=opts.config_dir
    )
    try:
        check_skills(AgentRegistry(resolver), SkillRegistry(resolver), report)  # type: ignore[arg-type]
    except ConfigError as e:
        report.err(str(e))  # type: ignore[attr-defined]


@app.command("check")
def check(
    ctx: typer.Context,
    preset: Annotated[str | None, typer.Option("--preset", help="Áp preset từ models.yaml")] = None,
    flag: Annotated[
        list[str] | None, typer.Option("--flag", "-f", help="VD -f --minimal -f --no-learn")
    ] = None,
) -> None:
    """Kiểm config: parse, Pydantic, compile expr, budget consistency, resolve alias, family."""
    try:
        cfg = _load(ctx, preset, flag or [])
    except ConfigError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=2) from None
    report = run_checks(cfg)  # type: ignore[arg-type]
    _check_skills(ctx, report)
    for line in report.notes:
        console.print(f"  [dim]{line}[/dim]")
    for line in report.warnings:
        console.print(f"  [yellow]⚠ {line}[/yellow]")
    for line in report.errors:
        console.print(f"  [red]✗ {line}[/red]")
    console.print(f"\nfingerprint {cfg.fingerprint[:19]}  sources {dict(cfg.sources)}")  # type: ignore[attr-defined]
    if report.ok:
        console.print(f"[green]✓ config OK[/green] ({len(report.warnings)} cảnh báo)")
    else:
        console.print(f"[red]✗ {len(report.errors)} lỗi[/red]")
        raise typer.Exit(code=2)


@app.command("show")
def show(
    ctx: typer.Context,
    preset: Annotated[str | None, typer.Option("--preset")] = None,
    flag: Annotated[list[str] | None, typer.Option("--flag", "-f")] = None,
    section: Annotated[str | None, typer.Argument(help="policy | budgets | models | sensitive_paths")] = None,
) -> None:
    """In config đã resolve (JSON)."""
    try:
        cfg = _load(ctx, preset, flag or [])
    except ConfigError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=2) from None
    if section:
        obj = getattr(cfg, section, None)
        if obj is None:
            console.print(f"[red]section lạ: {section}[/red]")
            raise typer.Exit(code=2)
        typer.echo(obj.model_dump_json(indent=2, by_alias=True))
    else:
        typer.echo(json.dumps(cfg.summary(), indent=2, ensure_ascii=False))  # type: ignore[attr-defined]


@app.command("roles")
def roles(ctx: typer.Context, preset: Annotated[str | None, typer.Option("--preset")] = None) -> None:
    """Bảng role → model thật sự sẽ dùng (resolve alias · provider bật · API key · khác họ · fallback)."""
    from digital_development_team.llm.registry import ModelRegistry

    opts: GlobalOpts = ctx.obj
    try:
        cfg = _load(ctx, preset, [])
    except ConfigError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=2) from None
    reg = ModelRegistry(cfg.models, provider_override=opts.provider)  # type: ignore[attr-defined]
    t = Table("role", "alias", "provider/model", "family", "key", "fallback", "ghi chú")
    for role in cfg.models.roles:  # type: ignore[attr-defined]
        try:
            r = reg.resolve(role)
        except ConfigError:
            rc = cfg.models.roles[role]  # type: ignore[attr-defined]
            ref = reg.alias_ref(rc.model)
            why = reg.unavailable_reason(rc.model) or "?"
            t.add_row(
                role,
                rc.model,
                f"{ref.provider}/{ref.model}" if ref else "-",
                ref.family if ref else "-",
                "[red]✗[/red]",
                rc.fallback or "",
                f"[red]{why}[/red]",
            )
            continue
        key = "✓" if reg.has_key(r.ref.provider) else "[red]✗[/red]"
        t.add_row(
            role,
            r.ref.alias,
            f"{r.ref.provider}/{r.ref.model}",
            r.ref.family,
            key,
            r.fallback.alias if r.fallback else "",
            "; ".join(r.notes),
        )
    console.print(t)

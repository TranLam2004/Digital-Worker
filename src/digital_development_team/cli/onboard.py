"""ddt onboard — nhận diện stack của repo đích, probe lệnh build/lint/typecheck/test, ghi REPO.md + SETUP.yaml."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.cli.run import _rt

console = Console()


def onboard_cmd(
    ctx: typer.Context,
    install: Annotated[
        bool, typer.Option("--install", help="Cài phụ thuộc trước khi probe (npm ci, go mod download…)")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Chỉ in kết quả, không ghi REPO.md/SETUP.yaml")
    ] = False,
    timeout: Annotated[int, typer.Option("--timeout", help="Giây tối đa mỗi lệnh")] = 600,
) -> None:
    """Probe lệnh build/lint/typecheck/test trong worktree tạm của HEAD (repo không bị sửa)."""
    from digital_development_team.isolation.git import Git
    from digital_development_team.onboarding import onboard
    from digital_development_team.sandbox import build_sandbox

    rt = _rt(ctx)
    try:
        if rt.repo is None:
            console.print("[red]✗ cần --repo[/red]")
            raise typer.Exit(code=2)
        scratch = rt.layout.state_dir / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        rep = onboard(
            rt.repo,
            git=Git(rt.repo, scratch=scratch),
            sandbox=build_sandbox(rt.config.policy.sandbox, scratch=scratch),
            sandbox_cfg=rt.config.policy.sandbox,
            memory_dir=rt.layout.memory_dir,
            scratch=scratch,
            install=install,
            dry_run=dry_run,
            timeout_s=float(timeout),
            log=lambda m: console.print(f"[dim]{m}[/dim]"),
        )
    finally:
        rt.close()
    console.print(
        "Ngôn ngữ: "
        + (
            ", ".join(f"{s.language}{' (' + s.root + ')' if s.root else ''}" for s in rep.stacks)
            or "không nhận ra"
        )
    )
    for tool, hint in rep.missing.items():
        console.print(f"[yellow]⚠ thiếu {tool}[/yellow] → {hint}")
    t = Table("Bước", "Lệnh", "Kết quả")
    for p in rep.restore_log + rep.probes:
        t.add_row(
            p.kind, p.cmd, ("[green]✓ " if p.ok else "[red]✗ ") + p.note + ("[/green]" if p.ok else "[/red]")
        )
    console.print(t)
    if rep.repo_md:
        console.print(f"[green]✓ ghi {rep.repo_md}[/green] và {rep.setup}")

"""`ddt skill` — xem/duyệt skill học được · `ddt learn` — hàng việc của Learner (runner/learn.py)."""

from __future__ import annotations

import difflib
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.cli.run import _rt
from digital_development_team.core.errors import DDTError

console = Console()
skill_app = typer.Typer(no_args_is_help=True)
learn_app = typer.Typer(no_args_is_help=True)


@skill_app.command("list")
def skill_list(
    ctx: typer.Context,
    status: Annotated[
        str | None, typer.Option("--status", help="quarantine · candidate · active · retired · rejected")
    ] = None,
) -> None:
    """Skill học được và số lần dùng/pass của bản hiện tại."""
    from digital_development_team.memory.learned import LearnedSkills

    rt = _rt(ctx)
    try:
        st = LearnedSkills(rt.db, rt.layout, rt.config.policy.memory, clock=rt.clock)
        rows = st.list_skills(status=status)
    finally:
        rt.close()
    if not rows:
        console.print("[dim]chưa có skill học được[/dim]")
        return
    t = Table("skill", "v", "trạng thái", "module/type", "pass", "conf", "mô tả")
    for s in rows:
        t.add_row(
            s.name,
            str(s.version),
            s.status,
            f"{s.module}/{s.issue_type}",
            f"{s.passed}/{s.applied}",
            f"{s.confidence:.2f}",
            s.description[:60],
        )
    console.print(t)


@skill_app.command("show")
def skill_show(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="learned-<slug>")],
    version: Annotated[int | None, typer.Option("--version", "-V")] = None,
) -> None:
    """Nội dung + lịch sử (đề xuất, quét, dùng, nâng, gỡ)."""
    from digital_development_team.memory.learned import LearnedSkills

    rt = _rt(ctx)
    try:
        st = LearnedSkills(rt.db, rt.layout, rt.config.policy.memory, clock=rt.clock)
        s = st.get(name)
        if s is None:
            console.print(f"[red]✗ không có {name}[/red]")
            raise typer.Exit(code=2)
        text = st.text(name, version)
        hist = st.history(name)
    finally:
        rt.close()
    console.print(
        f"[bold]{s.name}[/bold] v{s.version} · {s.status} · pass {s.passed}/{s.applied} · conf {s.confidence:.2f}"
    )
    if s.problems:
        console.print("[yellow]quét không qua:[/yellow] " + "; ".join(s.problems))
    console.print(text, markup=False, highlight=False)
    for h in hist[-15:]:
        console.print(
            f"[dim]{h['at'][:19]} v{h['version']} {h['event']} · {h['by_actor']}"
            + (f" · {h['issue_id']}" if h["issue_id"] else "")
            + (f" · {h['note']}" if h["note"] else "")
            + "[/dim]",
            highlight=False,
        )


@skill_app.command("diff")
def skill_diff(ctx: typer.Context, name: Annotated[str, typer.Argument(help="learned-<slug>")]) -> None:
    """So bản hiện tại với bản trước."""
    from digital_development_team.memory.learned import LearnedSkills

    rt = _rt(ctx)
    try:
        st = LearnedSkills(rt.db, rt.layout, rt.config.policy.memory, clock=rt.clock)
        s = st.get(name)
        if s is None or s.version < 2:
            console.print("[dim]chưa có bản trước để so[/dim]")
            return
        old, new = st.text(name, s.version - 1), st.text(name, s.version)
    finally:
        rt.close()
    for line in difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"v{s.version - 1}", f"v{s.version}", lineterm=""
    ):
        style = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
        console.print(f"[{style}]{line}[/{style}]", markup=True, highlight=False)


def _decide(ctx: typer.Context, name: str, action: str, note: str) -> None:
    from digital_development_team.memory.learned import LearnedSkills

    rt = _rt(ctx)
    try:
        st = LearnedSkills(rt.db, rt.layout, rt.config.policy.memory, clock=rt.clock)
        s = st.set_status(name, action, by="human:cli", note=note)
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
        raise typer.Exit(code=2) from None
    finally:
        rt.close()
    console.print(f"[green]✓[/green] {s.name} v{s.version} → {s.status}")


@skill_app.command("approve")
def skill_approve(ctx: typer.Context, name: str, note: Annotated[str, typer.Option("--note")] = "") -> None:
    """Dùng ngay như skill đã kiểm chứng (active)."""
    _decide(ctx, name, "approve", note)


@skill_app.command("reject")
def skill_reject(ctx: typer.Context, name: str, note: Annotated[str, typer.Option("--note")] = "") -> None:
    """Không bao giờ dùng; Learner không được đề xuất lại cùng tên."""
    _decide(ctx, name, "reject", note)


@skill_app.command("retire")
def skill_retire(ctx: typer.Context, name: str, note: Annotated[str, typer.Option("--note")] = "") -> None:
    """Gỡ khỏi brief (giữ lịch sử)."""
    _decide(ctx, name, "retire", note)


@learn_app.command("list")
def learn_list(
    ctx: typer.Context,
    status: Annotated[
        str | None, typer.Option("--status", help="pending · running · done · failed · skipped")
    ] = None,
) -> None:
    """Issue đang chờ / đã được Learner học."""
    from digital_development_team.runner.learn import jobs

    rt = _rt(ctx)
    try:
        rows = jobs(rt.services(with_agents=False), status=status)
    finally:
        rt.close()
    if not rows:
        console.print("[dim]không có job[/dim]")
        return
    t = Table("issue", "trạng thái", "lần", "lý do", "ghi chú")
    for r in rows:
        t.add_row(
            r["issue_id"], r["status"], str(r["attempts"]), ", ".join(r["reasons"]), str(r["note"] or "")[:60]
        )
    console.print(t)


@learn_app.command("run")
def learn_run(
    ctx: typer.Context,
    issue: Annotated[str | None, typer.Argument(help="DDT-0001 (bỏ trống: mọi job đang chờ)")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Tối đa bao nhiêu job")] = 5,
) -> None:
    """Chạy Learner ngay (gọi LLM). Worker vẫn tự chạy khi rảnh."""
    from digital_development_team.core.context import CancelToken
    from digital_development_team.runner.learn import LearnRunner

    rt = _rt(ctx)
    try:
        lr = LearnRunner(rt.services(with_agents=True))
        cancel = CancelToken()
        for _ in range(1 if issue else limit):
            res = lr.run_one(cancel, issue=issue)
            if res is None:
                console.print("[dim]không còn job[/dim]")
                break
            color = "green" if res[1] == "done" else "yellow"
            console.print(f"[{color}]{res[0]}: {res[1]}[/{color}]")
    except KeyboardInterrupt:
        console.print("[yellow]dừng[/yellow]")
    finally:
        rt.close()


@skill_app.command("import")
def skill_import(
    ctx: typer.Context,
    spec: Annotated[str, typer.Argument(help="github:<owner>/<repo>/<path>@<commit 40 hex>")],
    roles: Annotated[str, typer.Option("--roles", help="Role được nạp (qua skill.search/load)")] = "dev",
    force: Annotated[bool, typer.Option("--force", help="Thay skill imported cùng tên")] = False,
) -> None:
    """Lấy SKILL.md + LICENSE từ commit đã ghim, quét injection/secret/license, ghi vào overrides + skills.lock.json."""
    from digital_development_team.agents.skill_import import default_fetch, import_skill
    from digital_development_team.tools.catalog import ToolCatalog

    rt = _rt(ctx)
    try:
        net = rt.config.policy.network
        res = import_skill(
            spec,
            home=rt.layout.home,
            fetch=default_fetch(net),
            network=net,
            tool_names=ToolCatalog(rt.resolver).names(),
            roles=[r.strip() for r in roles.split(",") if r.strip()],
            now=rt.clock.now().isoformat(),
            force=force,
        )
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
        raise typer.Exit(code=2) from None
    except OSError as e:
        console.print(f"[red]✗ tải không được: {e}[/red]")
        raise typer.Exit(code=2) from None
    finally:
        rt.close()
    console.print(f"[green]✓[/green] {res.name} ({res.license}, ~{res.tokens} token) → {res.path}")
    console.print("[dim]trigger manual: agent tìm qua skill.search rồi skill.load[/dim]")
    for w in res.warnings:
        console.print(f"[yellow]⚠ {w}[/yellow]")

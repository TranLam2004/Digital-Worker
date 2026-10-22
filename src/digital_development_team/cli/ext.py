"""`ddt ext mcp …` — MCP server ngoài cho agent (Phase 10): add · sync · pin · list · remove.

Cấu hình ghi vào `<home>/overrides/config/runtime.yaml` (policy.mcp_client.servers) — hot-reload ở issue kế tiếp.
Quy trình: add (khai lệnh/url, role, tool + risk) → sync (khởi động server, chụp danh sách tool) → người xem → pin
(ghi sha schema). Chỉ tool của server đã pin, schema khớp mới tới được agent.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.core.errors import ConfigError, DDTError

console = Console()
ext_app = typer.Typer(no_args_is_help=True)
mcp_app = typer.Typer(no_args_is_help=True)
ext_app.add_typer(mcp_app, name="mcp", help="MCP server ngoài: add · sync · pin · list · remove")

DOTTED = "mcp_client.servers"


def _servers(ctx: typer.Context) -> tuple[Any, list[dict[str, Any]]]:
    from digital_development_team.config.loader import load_config

    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home()
    cfg = load_config(home=home, config_dir=opts.config_dir)
    return home, [s.model_dump(exclude_none=True) for s in cfg.policy.mcp_client.servers]


def _save(home: Any, servers: list[dict[str, Any]]) -> None:
    from digital_development_team.config.models import McpClientCfg
    from digital_development_team.config.runtime_patch import set_runtime, unset_runtime

    McpClientCfg.model_validate({"servers": servers})  # sai → ConfigError trước khi ghi
    if servers:
        set_runtime(home, "policy", DOTTED, servers)
    else:
        unset_runtime(home, "policy", DOTTED)


def _fail(e: Exception) -> None:
    hint = getattr(e, "hint", None)
    console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {hint}[/dim]" if hint else ""))
    raise typer.Exit(code=2) from None


def _kv(items: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items:
        k, sep, v = it.partition("=")
        if not sep or not k:
            raise ConfigError(f"{what} phải dạng KEY=VALUE: {it!r}")
        out[k.strip()] = v.strip()
    return out


@mcp_app.command("add")
def mcp_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Tên ngắn, chữ thường: docs, jira, figma…")],
    command: Annotated[
        str | None, typer.Option("--command", help="stdio: lệnh đầy đủ, gói phải ghim phiên bản")
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="http: URL https nằm trong policy.network.allow")
    ] = None,
    roles: Annotated[str, typer.Option("--roles", help="Role được dùng, cách nhau dấu phẩy")] = "dev",
    tool: Annotated[list[str], typer.Option("--tool", help="tên=read|soft_write|hard_write (lặp lại)")] = [],  # noqa: B006
    env: Annotated[list[str], typer.Option("--env", help="KEY=VALUE hoặc KEY=${BIEN_MAY} (lặp lại)")] = [],  # noqa: B006
    max_calls: Annotated[int, typer.Option("--max-calls", help="Trần lần gọi mỗi issue")] = 20,
) -> None:
    """Khai một server (chưa dùng được cho tới khi sync + pin)."""
    import shlex

    try:
        home, servers = _servers(ctx)
        entry: dict[str, Any] = {
            "name": name,
            "transport": "http" if url else "stdio",
            "roles": [r.strip() for r in roles.split(",") if r.strip()],
            "tools": _kv(tool, "--tool"),
            "env": _kv(env, "--env"),
            "max_calls_per_issue": max_calls,
        }
        if url:
            entry["url"] = url
        if command:
            entry["command"] = shlex.split(command, posix=True)
        servers = [s for s in servers if s.get("name") != name] + [entry]
        _save(home, servers)
    except (DDTError, ValueError) as e:
        _fail(e)
    console.print(
        f"[green]✓[/green] đã khai {name} — tiếp: `ddt ext mcp sync {name}` rồi `ddt ext mcp pin {name}`"
    )


@mcp_app.command("sync")
def mcp_sync(ctx: typer.Context, name: str) -> None:
    """Khởi động server, chụp danh sách tool + schema (chưa cấp cho agent)."""
    from digital_development_team.app import build_runtime
    from digital_development_team.tools.mcp_client import sync

    opts: GlobalOpts = ctx.obj
    rt = build_runtime(opts.resolved_home(), config_dir=opts.config_dir, repo=opts.repo)
    try:
        srv = next((s for s in rt.config.policy.mcp_client.servers if s.name == name), None)
        if srv is None:
            _fail(ConfigError(f"không có server {name}", hint="ddt ext mcp add trước"))
            return
        scratch = rt.layout.state_dir / "tmp"
        snap = sync(
            srv,
            cwd=rt.repo or rt.layout.home,
            scratch=scratch,
            network=rt.config.policy.network,
            state_dir=rt.layout.state_dir,
            now=rt.clock.now().isoformat(),
        )
    except Exception as e:  # server ngoài lỗi đủ kiểu — báo gọn cho người
        _fail(e)
        return
    finally:
        rt.close()
    t = Table("tool", "khai", "mô tả")
    for item in snap["tools"]:
        n = str(item.get("name"))
        t.add_row(n, srv.tools.get(n, "[dim]ẩn[/dim]"), str(item.get("description") or "")[:70])
    console.print(t)
    pinned = "khớp pin" if srv.pinned_sha == snap["sha256"] else "CHƯA pin / đã đổi"
    console.print(f"sha256 {snap['sha256'][:16]}… ({pinned}) — xem kỹ rồi `ddt ext mcp pin {name}`")


@mcp_app.command("pin")
def mcp_pin(ctx: typer.Context, name: str) -> None:
    """Ghim schema đã sync: từ giờ tool tới được agent; server đổi schema → bị khoá tới khi pin lại."""
    from digital_development_team.storage.layout import HomeLayout
    from digital_development_team.tools.mcp_client import read_snapshot

    try:
        home, servers = _servers(ctx)
        snap = read_snapshot(HomeLayout(home).state_dir, name)
        if snap is None:
            raise ConfigError(f"{name} chưa sync", hint=f"ddt ext mcp sync {name}")
        found = False
        for s in servers:
            if s.get("name") == name:
                s["pinned_sha"] = snap["sha256"]
                found = True
        if not found:
            raise ConfigError(f"không có server {name}")
        _save(home, servers)
    except (DDTError, ValueError) as e:
        _fail(e)
        return
    console.print(f"[green]✓[/green] {name} pin {snap['sha256'][:16]}…")


@mcp_app.command("remove")
def mcp_remove(ctx: typer.Context, name: str) -> None:
    try:
        home, servers = _servers(ctx)
        _save(home, [s for s in servers if s.get("name") != name])
    except (DDTError, ValueError) as e:
        _fail(e)
    console.print(f"[green]✓[/green] đã gỡ {name}")


@mcp_app.command("list")
def mcp_list(ctx: typer.Context) -> None:
    """Server đã khai, trạng thái sync/pin và tool agent thấy."""
    from digital_development_team.config.loader import load_config
    from digital_development_team.storage.layout import HomeLayout
    from digital_development_team.tools.mcp_client import read_snapshot, snapshot_ops

    opts: GlobalOpts = ctx.obj
    home = opts.resolved_home()
    try:
        cfg = load_config(home=home, config_dir=opts.config_dir).policy.mcp_client
    except DDTError as e:
        _fail(e)
        return
    state = HomeLayout(home).state_dir
    ops, warnings = snapshot_ops(cfg, state)
    if not cfg.servers:
        console.print("[dim]chưa khai MCP server nào (ddt ext mcp add)[/dim]")
        return
    t = Table("server", "kiểu", "role", "trạng thái", "tool agent thấy")
    for s in cfg.servers:
        snap = read_snapshot(state, s.name)
        status = (
            "chưa sync"
            if snap is None
            else "chưa pin"
            if not s.pinned_sha
            else "OK"
            if snap.get("sha256") == s.pinned_sha
            else "schema đổi"
        )
        mine = [op.name for op in ops if op.name.startswith(f"mcp.{s.name}__")]
        t.add_row(s.name, s.transport, ",".join(s.roles), status, ", ".join(mine) or "-")
    console.print(t)
    for w in warnings:
        console.print(f"[yellow]⚠ {w}[/yellow]")

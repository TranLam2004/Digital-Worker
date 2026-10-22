"""`ddt worker` — process chạy pipeline liên tục; theo dõi bằng `ddt watch` / `ddt board`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.cli.run import _rt
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import DDTError
from digital_development_team.storage.heartbeat import Heartbeat

console = Console()
app = typer.Typer()


@app.command("worker")
def worker(
    ctx: typer.Context,
    poll: Annotated[float, typer.Option("--poll", help="Giây giữa hai lần kiểm queue/gate khi rảnh")] = 2.0,
    stub_stages: Annotated[
        bool, typer.Option("--stub-stages", help="Mọi stage agent = stub (0 LLM)")
    ] = False,
    golden: Annotated[
        Path | None, typer.Option("--golden", help="Thư mục golden cho --provider mock")
    ] = None,
    until_idle: Annotated[
        bool, typer.Option("--until-idle", help="Thoát khi không còn việc (script/demo), thay vì chờ mãi")
    ] = False,
    max_loops: Annotated[int | None, typer.Option("--max-loops", hidden=True)] = None,
    wip: Annotated[int | None, typer.Option("--wip", help="WIP song song (giữ chỗ, v1 tuần tự)")] = None,
) -> None:
    """Vòng lặp sống lâu: lấy issue từ queue, chạy, park khi chờ gate, chạy tiếp khi người quyết trên web/CLI.

    STOP (`ddt stop` hoặc nút đỏ) dừng stage đang chạy trong < 5s; `ddt resume` → tiếp đúng stage.
    """
    from digital_development_team.runner import Runner, default_handlers
    from digital_development_team.runner.worker import Worker

    opts: GlobalOpts = ctx.obj
    rt = _rt(ctx, golden)
    # hot-reload: config (runtime.yaml) hoặc override JD/skill/checklist/tool đổi → worker dựng lại
    # runner giữa hai issue, emit config_reloaded. Không cần khởi động lại worker.
    from digital_development_team.app import PROVIDER_PRESETS
    from digital_development_team.config.loader import ResolvedConfig
    from digital_development_team.config.provider import ConfigProvider
    from digital_development_team.config.resources import overrides_fingerprint

    home = opts.resolved_home()
    cfg_provider = ConfigProvider(
        home=home,
        config_dir=opts.config_dir,
        flags=opts.flags,
        preset=PROVIDER_PRESETS.get(opts.provider) if opts.provider else None,
    )

    def make_runner(cfg: ResolvedConfig | None = None) -> Runner:
        live = rt.reloaded(cfg or cfg_provider.snapshot())
        return Runner(
            live.services(with_agents=not stub_stages),
            handlers=default_handlers(stub=stub_stages),
            stub=stub_stages,
            config_source=cfg_provider.snapshot,
        )

    hb = Heartbeat(rt.db, rt.clock, poll_s=poll)
    w = Worker(
        make_runner(),
        rt.services(with_agents=False),
        heartbeat=hb,
        poll_s=poll,
        wip=wip,
        config_source=cfg_provider.snapshot,
        assets_fingerprint=lambda: overrides_fingerprint(home, config_dir=opts.config_dir),
        rebuild=make_runner,
        learn=not stub_stages,  # rảnh thì Learner học từ issue đã xong (tắt bằng --no-learn)
    )
    cancel = CancelToken()
    console.print(
        f"[bold]ddt worker[/bold] home={home} poll={poll:g}s"
        + (" stub" if stub_stages else "")
        + f"  [dim]heartbeat {hb.worker_id} · Ctrl+C để dừng[/dim]"
    )
    try:
        rep = w.serve_forever(cancel, max_loops=max_loops, until_idle=until_idle)
    except KeyboardInterrupt:
        cancel.cancel("ctrl-c")
        console.print("[yellow]⏸ ctrl-c — checkpoint đã lưu, `ddt worker` lại để tiếp[/yellow]")
        raise typer.Exit(code=3) from None
    except DDTError as e:
        console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
        raise typer.Exit(code=2) from None
    finally:
        rt.close()
    console.print(
        f"[green]worker thoát[/green] ({rep.exit_reason}) · {rep.loops} tick · {rep.runs} lần chạy · "
        + (f"{rep.learned} lần học · " if rep.learned else "")
        + " · ".join(f"{k} {v}" for k, v in sorted(rep.statuses.items()))
    )

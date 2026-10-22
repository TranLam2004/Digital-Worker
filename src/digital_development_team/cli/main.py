"""Entry point `ddt`. Các nhóm lệnh đăng ký ở đây; từng nhóm thêm theo bước build."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from digital_development_team import __version__
from digital_development_team.cli.options import GlobalOpts

app = typer.Typer(
    name="ddt",
    help="DDT — Digital Development Team. Harness điều phối AI agent xử lý backlog trên repo có sẵn.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ddt {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    repo: Annotated[Path | None, typer.Option("--repo", "-r", help="Repo đích", exists=False)] = None,
    home: Annotated[
        Path | None, typer.Option("--home", help="DDT home (mặc định <repo-parent>/<repo>.ddt)")
    ] = None,
    config_dir: Annotated[
        Path | None, typer.Option("--config-dir", envvar="DDT_CONFIG_DIR", help="Override config")
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="mock | anthropic | openai | local | cli (Claude Code CLI) | codex (Codex CLI — bắt buộc khi thi)",
        ),
    ] = None,
    match: Annotated[
        bool,
        typer.Option(
            "--match",
            help="Chế độ thi đấu: không chờ người, tắt Learner/canary, GATE theo rule tới T1 (= --flag --match)",
        ),
    ] = False,
    flag: Annotated[
        list[str] | None,
        typer.Option("--flag", help="Cờ config, lặp được: --flag --no-canary --flag --economy"),
    ] = None,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
    _: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="In phiên bản")
    ] = False,
) -> None:
    flags = (*(["--match"] if match else []), *(flag or []))
    ctx.obj = GlobalOpts(
        repo=repo, home=home, config_dir=config_dir, provider=provider, verbose=verbose, flags=tuple(flags)
    )


def _register() -> None:
    """Import lười từng nhóm lệnh để `ddt --version` không kéo cả hệ thống."""
    from digital_development_team.cli import config as _config
    from digital_development_team.cli import debug as _debug
    from digital_development_team.cli import ext as _ext
    from digital_development_team.cli import ingest as _ingest
    from digital_development_team.cli import learn as _learn
    from digital_development_team.cli import match as _match
    from digital_development_team.cli import mcp_serve as _mcp
    from digital_development_team.cli import onboard as _onboard
    from digital_development_team.cli import replay as _replay
    from digital_development_team.cli import run as _run
    from digital_development_team.cli import runtime as _runtime
    from digital_development_team.cli import schema as _schema
    from digital_development_team.cli import watch as _watch
    from digital_development_team.cli import worker as _worker

    app.add_typer(_config.app, name="config", help="Kiểm/xem config đóng gói và override")
    app.add_typer(_schema.app, name="schema", help="Kiểm JSON Schema artifact")
    app.add_typer(_debug.app, name="debug", help="Thao tác tay blackboard/index/event")
    app.command("watch")(_watch.watch)
    app.command("board")(_watch.board)
    for name in ("run", "stop", "resume", "gates", "resolve", "reopen"):
        app.command(name)(getattr(_run, name))
    app.command("ingest")(_ingest.ingest)
    app.command("onboard")(_onboard.onboard_cmd)
    app.command("replay")(_replay.replay)
    app.command("report")(_replay.report)
    app.command("handover")(_replay.handover)
    app.command("perf")(_replay.perf)
    app.command("security-report")(_replay.security_report_cmd)
    app.command("mcp", hidden=True)(_mcp.mcp_)
    app.command("queue")(_ingest.queue_)
    app.command("worker")(_worker.worker)
    app.add_typer(_runtime.runtime_app, name="runtime", help="3 cần điều khiển qua runtime.yaml (hot-reload)")
    app.add_typer(_match.app, name="match", help="Thi đấu MTM: prepare · run · status · fake-server")
    app.add_typer(_learn.skill_app, name="skill", help="Skill học được: xem · duyệt · gỡ")
    app.add_typer(_learn.learn_app, name="learn", help="Hàng việc của Learner (học sau mỗi issue)")
    app.add_typer(
        _ext.ext_app, name="ext", help="Mở rộng: MCP server ngoài (add · sync · pin · list · remove)"
    )


_register()


def main() -> None:
    app()


if __name__ == "__main__":
    main()

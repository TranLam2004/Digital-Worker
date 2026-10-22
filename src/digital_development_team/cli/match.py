"""`ddt match …` — thi đấu MTM: chuẩn bị trước Start Match, chạy cả trận không người, xem trạng thái, server giả lập.

ddt --repo R --provider codex match prepare --match M-01        # trước Start Match: onboard, index, kiểm kết nối
ddt --repo R --provider codex match run --match M-01            # từ Start Match tới FINISHED (tự bật --match)
ddt --repo R --provider mock match run --fake testdata/matches/practice.json   # luyện tập không tốn trận thật
ddt --repo R match status                                       # item · trạng thái · nộp · điểm chấm
ddt match fake-server --scenario … --team-repo R                # Game Master giả lập qua HTTP
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from digital_development_team.cli.options import GlobalOpts
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import ConfigError, DDTError

console = Console()
app = typer.Typer(no_args_is_help=True, help="Thi đấu MTM: prepare · run · status · fake-server")


def _fail(e: DDTError) -> None:
    console.print(f"[red]✗ {e}[/red]" + (f"\n  [dim]→ {e.hint}[/dim]" if e.hint else ""))
    raise typer.Exit(code=2)


def _match_opts(ctx: typer.Context) -> GlobalOpts:
    """Thi đấu luôn ở autonomy autonomous (không cổng nào chờ người) — tự thêm --match nếu chưa có."""
    opts: GlobalOpts = ctx.obj
    if "--match" not in opts.flags:
        opts = dataclasses.replace(opts, flags=(*opts.flags, "--match"))
        ctx.obj = opts
    return opts


def _gamemaster(rt: object, match_id: str | None, fake: Path | None, base_url: str | None):  # type: ignore[no-untyped-def]
    from digital_development_team.gamemaster import FakeGameMaster, HttpGameMaster

    if fake is not None:
        gm = FakeGameMaster.from_file(fake, clock=rt.clock, team_repo=rt.repo)  # type: ignore[attr-defined]
        gm.start_match()  # luyện tập: Start Match ngay
        return gm
    if not match_id:
        raise typer.BadParameter("cần --match <id> (hoặc --fake <kịch bản> để luyện tập)")
    cfg = rt.config.policy.gamemaster  # type: ignore[attr-defined]
    if cfg.adapter_factory:
        from digital_development_team.gamemaster.player import load_player

        return load_player(cfg, match_id, clock=rt.clock, repo=rt.repo)  # type: ignore[attr-defined]
    if base_url:
        cfg = cfg.model_copy(update={"base_url": base_url})
    return HttpGameMaster(cfg, match_id, now=rt.clock.now)  # type: ignore[attr-defined]


def _check_contest_models(rt: object) -> None:
    """All enabled role choices, including fallbacks, must remain on Codex/GPT."""
    registry = rt.models  # type: ignore[attr-defined]
    for role in registry.config.roles:
        resolution = registry.resolve(role)
        for ref in (resolution.ref, resolution.fallback):
            if ref is None:
                continue
            if registry.config.providers[ref.provider].adapter != "codex_cli" or ref.family != "gpt":
                raise ConfigError(f"Contest role {role} must use GPT through Codex CLI, including fallback")


def _check_team_repo(rt: object, match_id: str, *, verify_remote: bool = True) -> str:
    from digital_development_team.isolation.git import Git

    repo = rt.repo  # type: ignore[attr-defined]
    if repo is None:
        raise ConfigError("Contest requires --repo pointing to the team Git root")
    cfg = rt.config.policy.gamemaster  # type: ignore[attr-defined]
    folder = cfg.match_folder.format(match_id=match_id)
    root = repo.resolve()
    target = (root / folder).resolve()
    if not target.is_relative_to(root) or not target.is_dir():
        raise ConfigError("Match source folder must exist inside the team repository; pull it before prepare")
    git = Git(root, scratch=rt.layout.state_dir / "tmp")  # type: ignore[attr-defined]
    top = Path(git.run(["rev-parse", "--show-toplevel"]).check("repo root").stdout.strip()).resolve()
    if top != root or git.default_branch() != "main":
        raise ConfigError("Contest --repo must be the team Git root checked out on main")
    if not cfg.push_remote or not git.has_remote(cfg.push_remote):
        raise ConfigError("Contest requires a configured team Git remote")
    if verify_remote:
        git.verify_pushed(cfg.push_remote, "main", git.head())
    return str(target.relative_to(root).as_posix())


@app.command("prepare")
def prepare(
    ctx: typer.Context,
    match_id: Annotated[str | None, typer.Option("--match", help="Match ID trên Game Master")] = None,
    base_url: Annotated[str | None, typer.Option("--url", help="Ghi đè gamemaster.base_url")] = None,
    fake: Annotated[Path | None, typer.Option("--fake", help="Kịch bản giả lập (luyện tập)")] = None,
    skip_onboard: Annotated[bool, typer.Option("--skip-onboard")] = False,
) -> None:
    """Chặng Chuẩn bị (người còn ở máy): kiểm Game Master + token, onboard repo (REPO.md, lệnh test), index code,
    gọi thử model. Mọi thứ ở đây được phép làm TRƯỚC Start Match và giúp trận chạy nhanh hơn."""
    from digital_development_team.cli.run import _rt

    _match_opts(ctx)
    rt = _rt(ctx)
    try:
        if rt.repo is None:
            console.print("[red]✗ cần --repo (repo của đội, đã pull bản mới nhất)[/red]")
            raise typer.Exit(code=2)
        gm = _gamemaster(rt, match_id, fake, base_url)
        snap = gm.snapshot()
        scope_dir = None
        if fake is None:
            _check_contest_models(rt)
            scope_dir = _check_team_repo(rt, gm.match_id)
            if snap.status.value not in ("READY", "PREPARING"):
                raise ConfigError("Run prepare only before Start Match")
        console.print(
            f"[green]✓ Game Master[/green] {snap.match_id} · {snap.status.value} · "
            f"{len(snap.items)} item đang hiện · brief: {snap.brief.get('name', '—')}"
        )
        if not skip_onboard:
            from digital_development_team.isolation.git import Git
            from digital_development_team.onboarding import onboard
            from digital_development_team.sandbox import build_sandbox

            scratch = rt.layout.state_dir / "tmp"
            scratch.mkdir(parents=True, exist_ok=True)
            rep = onboard(
                rt.repo,
                git=Git(rt.repo, scratch=scratch),
                sandbox=build_sandbox(rt.config.policy.sandbox, scratch=scratch),
                sandbox_cfg=rt.config.policy.sandbox,
                memory_dir=rt.layout.memory_dir,
                scratch=scratch,
                install=True,
                dry_run=False,
                timeout_s=600.0,
                log=lambda m: console.print(f"[dim]{m}[/dim]"),
                scope_dir=scope_dir,
            )
            ok = sum(1 for p in rep.probes if p.ok)
            if rep.missing or not rep.chosen.get("test"):
                raise ConfigError(
                    "Onboarding has missing tools or no executable test command; resolve before Start"
                )
            console.print(
                f"[green]✓ onboard[/green] {ok}/{len(rep.probes)} lệnh chạy được · {rep.repo_md or 'không ghi REPO.md'}"
            )
        t0 = time.monotonic()
        from digital_development_team.core.llm import ChatRequest, Message

        checked = set()
        for role in rt.config.models.roles:
            client = rt.models.client(role)
            key = (client.ref.provider, client.ref.model, client.ref.effort)
            if key in checked:
                continue
            response = client.chat(
                ChatRequest(model=client.ref, system="Reply with OK.", messages=(Message.user_text("ping"),)),
                cancel=CancelToken(),
            )
            if response.fell_back:
                raise ConfigError(f"Model preflight fell back for {role}; verify account model access")
            checked.add(key)
            console.print(
                f"[green]✓ model[/green] {client.ref.provider}/{client.ref.model}/{client.ref.effort}"
            )
        console.print(f"Model preflight: {len(checked)} cấu hình · {time.monotonic() - t0:.1f}s")
    except DDTError as e:
        _fail(e)
    finally:
        rt.close()
    console.print(
        "[bold]Preflight cục bộ hoàn tất.[/bold] Xác nhận READY bằng Game Player chính thức trước Start Match."
    )


@app.command("run")
def run(
    ctx: typer.Context,
    match_id: Annotated[str | None, typer.Option("--match", help="Match ID trên Game Master")] = None,
    base_url: Annotated[str | None, typer.Option("--url", help="Ghi đè gamemaster.base_url")] = None,
    fake: Annotated[Path | None, typer.Option("--fake", help="Kịch bản giả lập (luyện tập)")] = None,
    slots: Annotated[
        int, typer.Option("--slots", min=1, help="Số issue chạy song song (chưa kể slot PI)")
    ] = 1,
    stub_stages: Annotated[
        bool, typer.Option("--stub-stages", help="Stage agent = stub (0 LLM, kiểm luồng)")
    ] = False,
    golden: Annotated[Path | None, typer.Option("--golden", help="Golden cho --provider mock")] = None,
    max_ticks: Annotated[int | None, typer.Option("--max-ticks", hidden=True)] = None,
) -> None:
    """Từ Start Match tới FINISHED: theo dõi trang match, xếp lịch, làm, push, nộp — không cần người."""
    from digital_development_team.cli.run import _rt
    from digital_development_team.runner import Runner, default_handlers
    from digital_development_team.runner.match_runner import MatchRunner

    _match_opts(ctx)
    rt = _rt(ctx, golden)
    cancel = CancelToken()
    try:
        gm = _gamemaster(rt, match_id, fake, base_url)
        if fake is None:
            if stub_stages or golden is not None:
                raise ConfigError("Simulated agents are allowed only with --fake")
            _check_contest_models(rt)
            _check_team_repo(rt, gm.match_id, verify_remote=False)
        gcfg = rt.config.policy.gamemaster
        svc = dataclasses.replace(rt.services(with_agents=not stub_stages), gamemaster=gm)

        def make_runner() -> Runner:
            slot_svc = dataclasses.replace(rt.services(with_agents=not stub_stages), gamemaster=gm)
            return Runner(slot_svc, handlers=default_handlers(stub=stub_stages), stub=stub_stages)

        mr = MatchRunner(
            svc,
            gm,
            make_runner,
            slots=slots,
            pi_slot=gcfg.pi_slot,
            poll_s=gcfg.poll_s,
            grade_poll_s=gcfg.grade_poll_s,
        )
        console.print(
            f"[bold]ddt match run[/bold] {gm.match_id} · {slots} slot"
            + (" + 1 slot PI" if gcfg.pi_slot else "")
        )
        rep = mr.run(cancel, max_ticks=max_ticks)
    except KeyboardInterrupt:
        cancel.cancel("ctrl-c")
        console.print("[yellow]⏸ ctrl-c — checkpoint đã lưu, `ddt match run` lại để tiếp[/yellow]")
        raise typer.Exit(code=3) from None
    except DDTError as e:
        _fail(e)
    finally:
        rt.close()
    console.print(
        f"[green]kết thúc[/green] ({rep.exit_reason}) · {rep.ticks} tick · {rep.dispatched} lần giao việc · "
        + " · ".join(f"{k} {v}" for k, v in sorted({**rep.statuses, **rep.events}.items()))
    )


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Bảng item thi đấu: issue · loại · điểm · trạng thái pipeline · nộp · kết quả chấm."""
    from digital_development_team.cli.run import _rt
    from digital_development_team.ingest.gamemaster_source import SOURCE
    from digital_development_team.ingest.queue import Queue
    from digital_development_team.runner.match import read_match

    rt = _rt(ctx)
    try:
        svc = rt.services(with_agents=False)
        t = Table("Item", "Issue", "Loại", "Điểm", "Pipeline", "Nộp", "Chấm", "Hạn")
        for row in Queue(rt.db, rt.clock).list():
            if row.source != SOURCE:
                continue
            m = read_match(svc, row.issue_id)
            cp = rt.checkpoints.load(row.issue_id)
            sub = m.get("submission") or {}
            t.add_row(
                row.source_id,
                row.issue_id,
                str(m.get("type", "")),
                f"{m.get('points') or ''}",
                f"{cp.stage.value if cp else '—'} / {row.status}",
                str(sub.get("status") or "—"),
                f"{sub.get('grade') or '—'} {sub.get('score') if sub.get('score') is not None else ''}",
                str(m.get("deadline_at") or "—")[11:19],
            )
        console.print(t)
    finally:
        rt.close()


@app.command("fake-server")
def fake_server(
    scenario: Annotated[Path, typer.Option("--scenario", exists=True, dir_okay=False)],
    team_repo: Annotated[Path | None, typer.Option("--team-repo", help="Repo đội để Grader giả chấm")] = None,
    port: Annotated[int, typer.Option("--port")] = 8765,
    start: Annotated[bool, typer.Option("--start/--no-start", help="Start Match ngay")] = True,
) -> None:
    """Game Master giả lập qua HTTP (luyện tập HttpGameMaster): base_url = http://127.0.0.1:<port>."""
    from digital_development_team.gamemaster import FakeGameMaster
    from digital_development_team.gamemaster.fake_server import make_server

    fake = FakeGameMaster.from_file(scenario, team_repo=team_repo, grade_async=True)
    if start:
        fake.start_match()
    srv = make_server(fake, port=port)
    console.print(
        f"[bold]Game Master giả lập[/bold] http://127.0.0.1:{port} · {fake.match_id} · Ctrl+C để dừng"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()

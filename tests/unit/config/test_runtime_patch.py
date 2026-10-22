"""config.runtime_patch — lớp 3b: patch dotted, preset/flags mặc định, ConfigProvider thấy mtime, worker emit config_reloaded."""

from __future__ import annotations

from pathlib import Path

import pytest

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.config.provider import ConfigProvider
from digital_development_team.config.runtime_patch import (
    clear_runtime,
    read_runtime,
    runtime_path,
    set_runtime,
    unset_runtime,
    write_runtime,
)
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import ConfigError
from digital_development_team.eventlog.reader import read_events
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.worker import Worker
from tests.conftest import TESTDATA


def test_set_unset_and_loader_applies(tmp_home: Path) -> None:
    assert read_runtime(tmp_home) == {}
    set_runtime(tmp_home, "policy", "autonomy", "aggressive")
    set_runtime(tmp_home, "policy", "wip_limit", 2)
    set_runtime(tmp_home, "budgets", "stages.IMPLEMENT.T1.turn", 7)
    assert runtime_path(tmp_home).is_file()
    cfg = load_config(home=tmp_home)
    assert cfg.policy.autonomy == "aggressive" and cfg.policy.wip_limit == 2
    assert cfg.budgets.stages["IMPLEMENT"].T1.turn == 7
    assert any(p.startswith("runtime:policy:autonomy") for p in cfg.patches)
    # preset + flags mặc định từ runtime
    set_runtime(tmp_home, "preset", None, "economy")
    set_runtime(tmp_home, "flags", None, ["--no-canary"])
    cfg = load_config(home=tmp_home)
    assert "runtime:preset:economy" in cfg.patches and "flag:--no-canary" in cfg.patches
    assert cfg.policy.canary.enabled is False
    # CLI preset thắng runtime preset
    cfg2 = load_config(home=tmp_home, preset="mixed_verifier")
    assert "runtime:preset:economy" not in cfg2.patches
    unset_runtime(tmp_home, "policy", "wip_limit")
    assert load_config(home=tmp_home).policy.wip_limit == 3
    unset_runtime(tmp_home, "preset")
    unset_runtime(tmp_home, "flags")
    assert "preset" not in read_runtime(tmp_home)
    assert clear_runtime(tmp_home) and not runtime_path(tmp_home).is_file()
    assert read_runtime(tmp_home) == {}


def test_bad_runtime_is_config_error(tmp_home: Path) -> None:
    write_runtime(tmp_home, {"policy": {"wip_limit": "ba"}})
    with pytest.raises(ConfigError):
        load_config(home=tmp_home)
    runtime_path(tmp_home).write_text("weird: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="key lạ"):
        read_runtime(tmp_home)
    with pytest.raises(ConfigError):
        set_runtime(tmp_home, "nope", "x", 1)


def test_provider_reloads_on_runtime_change(tmp_home: Path) -> None:
    prov = ConfigProvider(home=tmp_home, config_dir=None)
    seen: list[str] = []
    prov2 = ConfigProvider(home=tmp_home, config_dir=None, on_reload=lambda c: seen.append(c.policy.autonomy))
    a = prov.snapshot()
    prov2.snapshot()
    assert a.policy.autonomy == "balanced"
    set_runtime(tmp_home, "policy", "autonomy", "conservative")
    b = prov.snapshot()
    assert b.policy.autonomy == "conservative" and b.fingerprint != a.fingerprint
    prov2.snapshot()
    assert seen == ["conservative"]
    # file hỏng → giữ bản cũ
    runtime_path(tmp_home).write_text("policy: {wip_limit: ba}\n", encoding="utf-8")
    assert prov.snapshot().policy.autonomy == "conservative"


def test_worker_picks_up_runtime_patch_at_next_issue(runtime: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    prov = ConfigProvider(home=runtime.home, config_dir=None)
    svc = runtime.services(with_agents=False)
    runner = Runner(svc, handlers=default_handlers(stub=True), stub=True, config_source=prov.snapshot)
    w = Worker(runner, svc, poll_s=0.01, config_source=prov.snapshot)
    rep0 = w.serve_forever(CancelToken(), max_loops=1)  # issue đầu qua worker → worker ghi nhớ fingerprint
    assert rep0.statuses["done"] == 1
    set_runtime(runtime.home, "policy", "wip_limit", 1)  # web Settings ghi
    rep = w.serve_forever(CancelToken(), until_idle=True)
    assert rep.statuses["done"] >= 1
    # issue kế (DDT-0002 sau khi 0001 xong) thấy fingerprint mới → config_reloaded
    evs = read_events(runtime.layout, "DDT-0002")
    assert any(e.event.value == "config_reloaded" for e in evs), [e.event.value for e in evs][:8]

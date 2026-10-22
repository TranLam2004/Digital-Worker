from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from digital_development_team.core.context import CancelToken, FakeClock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "digital_development_team"
TESTDATA = ROOT / "testdata"


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 3, 9, 0, tzinfo=UTC))


@pytest.fixture
def cancel() -> CancelToken:
    return CancelToken()


@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    home = tmp_path / "home.ddt"
    home.mkdir()
    return home


@pytest.fixture
def runtime(tmp_home: Path, fake_clock: FakeClock):  # type: ignore[no-untyped-def]
    """Runtime thật trên home tạm, clock giả, config đóng gói."""
    from digital_development_team.app import build_runtime

    rt = build_runtime(tmp_home, clock=fake_clock)
    yield rt
    rt.close()

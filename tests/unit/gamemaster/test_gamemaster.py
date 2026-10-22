"""Game Master: bản giả lập đúng luật v3.5 · client HTTP qua server giả · parse khoan dung."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from digital_development_team.config.models import GameMasterCfg
from digital_development_team.core.context import FakeClock
from digital_development_team.gamemaster import (
    FakeGameMaster,
    GameMasterError,
    GradeStatus,
    HttpGameMaster,
    ItemStatus,
    ItemType,
    MatchStatus,
)
from digital_development_team.gamemaster.fake_server import serve_in_thread
from digital_development_team.gamemaster.models import parse_item, parse_snapshot
from tests.conftest import TESTDATA

SCENARIO = TESTDATA / "matches" / "practice_calc.json"
SHA = "a" * 40


def _fake(clock: FakeClock) -> FakeGameMaster:
    gm = FakeGameMaster.from_file(SCENARIO, clock=clock)
    gm.start_match()
    return gm


def test_reveal_schedule_and_mid_match_change(fake_clock: FakeClock) -> None:
    gm = FakeGameMaster.from_file(SCENARIO, clock=fake_clock)
    assert gm.snapshot().items == () and gm.snapshot().status is MatchStatus.READY  # chưa Start Match
    gm.start_match()
    assert [i.id for i in gm.snapshot().items] == ["BUG-01"]
    fake_clock.advance(301)
    snap = gm.snapshot()
    assert [i.id for i in snap.items] == ["BUG-01", "CR-01"] and snap.item("CR-01").type is ItemType.CR  # type: ignore[union-attr]
    before = snap.item("CR-01").fingerprint()  # type: ignore[union-attr]
    fake_clock.advance(300)
    assert gm.snapshot().item("CR-01").fingerprint() != before  # type: ignore[union-attr]  # đề đổi lúc 600 s


def test_mvs_only_after_start_and_submit_rules(fake_clock: FakeClock) -> None:
    gm = _fake(fake_clock)
    with pytest.raises(GameMasterError) as e:
        gm.submit("BUG-01", git_revision=SHA, answer={}, idempotency_key="k1")
    assert e.value.status == 409  # chưa start working
    with pytest.raises(GameMasterError):
        gm.start_working("CR-01")  # chưa reveal → không lấy được đề/MVS trước
    st = gm.start_working("BUG-01")
    assert len(st.mvs) == 2 and "rootCause" in st.submit_template
    assert gm.start_working("BUG-01") == st  # gọi lại: cùng thông tin
    with pytest.raises(GameMasterError):
        gm.submit("BUG-01", git_revision="abc", answer={}, idempotency_key="k1")  # không phải full SHA
    r1 = gm.submit("BUG-01", git_revision=SHA, answer={"summary": "x"}, idempotency_key="k1")
    assert (
        r1.accepted
        and gm.submit("BUG-01", git_revision=SHA, answer={"summary": "x"}, idempotency_key="k1") == r1
    )
    with pytest.raises(GameMasterError) as e2:
        gm.submit("BUG-01", git_revision=SHA, answer={"summary": "y"}, idempotency_key="k2")
    assert e2.value.status == 409  # mỗi item nộp 1 lần
    assert gm.submission("BUG-01", r1.submission_id).status is GradeStatus.PASSED  # không lệnh chấm → đạt


def test_deadline_closes_and_pause_is_not_counted(fake_clock: FakeClock) -> None:
    gm = _fake(fake_clock)
    first = gm.snapshot().item("BUG-01")
    assert first is not None and first.deadline_at is not None
    gm.pause()
    fake_clock.advance(600)
    assert gm.snapshot().status is MatchStatus.PAUSED
    gm.resume()
    after = gm.snapshot().item("BUG-01")
    assert after is not None and after.deadline_at == first.deadline_at + timedelta(seconds=600)  # hạn lùi
    fake_clock.advance(25 * 60 + 1)
    assert gm.snapshot().item("BUG-01").status is ItemStatus.CLOSED  # type: ignore[union-attr]
    with pytest.raises(GameMasterError):
        gm.start_working("BUG-01")


def test_match_finishes_after_duration(fake_clock: FakeClock) -> None:
    gm = _fake(fake_clock)
    fake_clock.advance(40 * 60)
    assert gm.snapshot().status is MatchStatus.FINISHED


def test_http_client_against_fake_server(fake_clock: FakeClock) -> None:
    gm = _fake(fake_clock)
    srv, url = serve_in_thread(gm)
    try:
        client = HttpGameMaster(
            GameMasterCfg(base_url=url, retries=0),
            gm.match_id,
            env={"DDT_GM_TOKEN": "t0k"},
            now=fake_clock.now,
        )
        snap = client.snapshot()
        assert snap.match_id == "M-PRACTICE-CALC" and snap.brief["name"] == "mini_calc"
        assert [(i.id, i.type, i.points) for i in snap.items] == [("BUG-01", ItemType.BUG, 200.0)]
        st = client.start_working("BUG-01")
        assert len(st.mvs) == 2
        r = client.submit("BUG-01", git_revision=SHA, answer={"summary": "fix"}, idempotency_key="k")
        assert (
            r.accepted
            and client.submit(
                "BUG-01", git_revision=SHA, answer={"summary": "fix"}, idempotency_key="k"
            ).submission_id
            == r.submission_id
        )
        with pytest.raises(GameMasterError) as e:
            client.submit("BUG-01", git_revision=SHA, answer={"summary": "other"}, idempotency_key="k2")
        assert e.value.status == 409
        assert client.submission("BUG-01", r.submission_id).status is GradeStatus.PASSED
    finally:
        srv.shutdown()


def test_http_client_refuses_non_http_base_url() -> None:
    from digital_development_team.core.errors import ConfigError

    with pytest.raises(ConfigError):
        HttpGameMaster(GameMasterCfg(base_url="file:///etc/passwd"), "M")
    with pytest.raises(ConfigError):
        HttpGameMaster(GameMasterCfg(), "M")  # chưa cấu hình base_url


def test_http_retry_after_then_success() -> None:
    import io
    import urllib.error

    calls: list[str] = []

    class Resp(io.BytesIO):
        status = 200
        headers: ClassVar[dict[str, str]] = {"ETag": "v1"}

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    def opener(req: Any, timeout: float, context: Any) -> Resp:
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, None)  # type: ignore[arg-type]
        return Resp(
            json.dumps(
                {"status": "IN_PROGRESS", "serverTime": "2026-10-17T00:00:00Z", "backlog": []}
            ).encode()
        )

    slept: list[float] = []
    c = HttpGameMaster(
        GameMasterCfg(base_url="https://gm.test/api", http_contract_confirmed=True),
        "M-1",
        env={},
        opener=opener,
        sleep=slept.append,
    )
    assert c.snapshot().status is MatchStatus.IN_PROGRESS
    assert calls == ["https://gm.test/api/matches/M-1"] * 2 and slept == [0.0]


def test_parse_is_tolerant_of_field_names() -> None:
    it = parse_item(
        {"backlogId": "PI-02", "maxPoints": 150, "revealedAt": "2026-11-10T09:00:00Z", "deadlineMinutes": 15}
    )
    assert it.type is ItemType.PI and it.points == 150.0
    assert it.deadline_at == datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
    snap = parse_snapshot(
        {"state": "paused", "serverTime": "2026-10-17T00:00:00Z", "items": [{"id": "TASK-01"}]},
        match_id="M",
        local_now=datetime.now(UTC),
    )
    assert snap.status is MatchStatus.PAUSED and snap.items[0].type is ItemType.TASK


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "IN_PROGRESS"},
        {"status": "MYSTERY", "serverTime": "2026-10-17T00:00:00Z"},
        {"status": "IN_PROGRESS", "serverTime": "bad"},
    ],
)
def test_snapshot_missing_authoritative_state_fails_closed(payload: dict) -> None:
    from digital_development_team.gamemaster.models import GameMasterProtocolError

    with pytest.raises(GameMasterProtocolError):
        parse_snapshot(payload, match_id="M", local_now=datetime.now(UTC))


@pytest.mark.parametrize("payload", [{}, {"status": "ACCEPTED"}, {"id": "S"}, {"status": "OK", "id": "S"}])
def test_submission_never_invents_acceptance(payload: dict) -> None:
    from digital_development_team.gamemaster.models import GameMasterProtocolError, parse_submit

    with pytest.raises(GameMasterProtocolError):
        parse_submit(payload, "BUG-1")


def test_auth_is_scoped_to_explicitly_authorized_actions() -> None:
    cfg = GameMasterCfg(base_url="http://127.0.0.1:8765", bearer_actions=["submit"])
    client = HttpGameMaster(cfg, "M", env={"DDT_GM_TOKEN": "test-value"})
    assert "Authorization" not in client._headers({}, "match")
    assert "Authorization" not in client._headers({}, "submission")
    assert client._headers({}, "submit")["Authorization"] == "Bearer test-value"


def test_unconfirmed_remote_http_contract_is_blocked() -> None:
    from digital_development_team.core.errors import ConfigError

    with pytest.raises(ConfigError, match="contract"):
        HttpGameMaster(GameMasterCfg(base_url="https://gm.test"), "M")


def test_fake_rejects_new_mutations_while_paused(fake_clock: FakeClock) -> None:
    gm = _fake(fake_clock)
    gm.start_working("BUG-01")
    gm.pause()
    with pytest.raises(GameMasterError):
        gm.start_working("BUG-01")
    with pytest.raises(GameMasterError):
        gm.submit("BUG-01", git_revision=SHA, answer={}, idempotency_key="k")


def test_mutation_retry_returns_to_runner_for_fresh_match_check() -> None:
    import urllib.error

    calls = []

    def unavailable(req, **kwargs):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 503, "offline", {}, None)

    client = HttpGameMaster(GameMasterCfg(base_url="http://127.0.0.1:8765", retries=3), "M", opener=unavailable)
    with pytest.raises(GameMasterError):
        client.submit("BUG-1", git_revision=SHA, answer={}, idempotency_key="fixed")
    assert len(calls) == 1  # runner checks PAUSED/closed before retrying the same request

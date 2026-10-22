from __future__ import annotations

import pytest

from digital_development_team.config.models import PriceCfg, RateCfg
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.errors import (
    Cancelled,
    ConfigError,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from digital_development_team.core.llm import Usage
from digital_development_team.llm.pricing import cost_usd
from digital_development_team.llm.retry import RateLimiter, RetryBudget, call_with_retry
from digital_development_team.llm.toolnames import to_canonical, to_wire


def test_toolnames_roundtrip() -> None:
    assert to_wire("fs.read") == "fs_read"
    assert to_wire("blackboard.write") == "blackboard_write"
    assert to_canonical("fs_read") == "fs.read"
    assert to_canonical("blackboard_write") == "blackboard.write"
    assert to_canonical("test_run_one") == "test.run_one"
    with pytest.raises(ConfigError):
        to_wire("fs_read")
    with pytest.raises(ConfigError):
        to_canonical("fsread")


def test_pricing() -> None:
    pricing = {
        "m": PriceCfg.model_validate({"in": 3.0, "out": 15.0, "cached_in": 0.3}),
        "n": PriceCfg.model_validate({"in": 1.0, "out": 2.0}),
    }
    u = Usage(tokens_in=1_000_000, tokens_out=100_000, tokens_cached=500_000)
    # 500k uncached × 3 + 500k cached × 0.3 + 100k × 15 = 1.5 + 0.15 + 1.5
    assert cost_usd("m", u, pricing) == pytest.approx(3.15)
    assert cost_usd("n", Usage(tokens_in=1_000_000, tokens_cached=1_000_000), pricing) == pytest.approx(
        1.0
    )  # không cached_in → giá in
    assert cost_usd("unknown", u, pricing) == 0.0


def test_rate_limiter_window(fake_clock: FakeClock) -> None:
    lim = RateLimiter(2, clock=fake_clock)
    c = CancelToken()
    lim.acquire("a", cancel=c)
    lim.acquire("a", cancel=c)
    assert lim.wait_time("a") == pytest.approx(60.0)
    assert lim.wait_time("b") == 0.0  # per provider
    fake_clock.advance(30)
    assert lim.wait_time("a") == pytest.approx(30.0)
    fake_clock.advance(31)
    assert lim.wait_time("a") == 0.0


def test_rate_limiter_cancel_while_waiting(fake_clock: FakeClock) -> None:
    lim = RateLimiter(1, clock=fake_clock)
    c = CancelToken()
    lim.acquire("a", cancel=c)
    c.cancel("stop")
    with pytest.raises(Cancelled):
        lim.acquire("a", cancel=c)


def test_retry_then_success() -> None:
    rate = RateCfg(backoff=[0.0, 0.0], on_429_consecutive=3)
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimited("429")
        return "ok"

    seen: list[str] = []
    b = RetryBudget(rate)
    assert (
        call_with_retry(fn, budget=b, cancel=CancelToken(), on_retry=lambda e, d: seen.append(e.code)) == "ok"
    )
    assert seen == ["RATE_LIMITED", "RATE_LIMITED"] and b.attempts == 3 and b.consecutive == 2


def test_retry_gives_up_after_consecutive() -> None:
    rate = RateCfg(backoff=[0.0], on_429_consecutive=2)
    b = RetryBudget(rate)

    def fn() -> str:
        raise ProviderUnavailable("down")

    with pytest.raises(ProviderUnavailable):
        call_with_retry(fn, budget=b, cancel=CancelToken())
    assert b.consecutive == 2 and b.should_fallback


def test_retry_non_retryable_raises_immediately() -> None:
    b = RetryBudget(RateCfg(backoff=[0.0]))
    n = {"c": 0}

    def fn() -> str:
        n["c"] += 1
        raise ProviderError("400")

    with pytest.raises(ProviderError):
        call_with_retry(fn, budget=b, cancel=CancelToken())
    assert n["c"] == 1

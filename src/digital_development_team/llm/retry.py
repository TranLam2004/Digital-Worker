"""Retry tập trung (SDK max_retries=0) + rate limiter rpm per provider.

Lỗi retryable: RateLimited · ProviderUnavailable. Sau `on_429_consecutive` lần liên tiếp → báo caller đổi fallback.
Backoff theo rate.backoff (giây), chờ qua cancel.wait → hủy giữa chừng dừng ngay.
tpm_cap: TODO(P2) — cần ước token trước call.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

from digital_development_team.config.models import RateCfg
from digital_development_team.core.context import CancelToken, Clock, SystemClock
from digital_development_team.core.errors import Cancelled, ProviderError

_WINDOW_S = 60.0


class RateLimiter:
    """Cửa sổ trượt 60s theo provider. acquire() chặn (qua cancel.wait) khi vượt rpm_cap."""

    def __init__(self, rpm_cap: int, *, clock: Clock | None = None, per_provider: bool = True) -> None:
        self._cap = max(rpm_cap, 1)
        self._clock = clock or SystemClock()
        self._per_provider = per_provider
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    def _key(self, provider: str) -> str:
        return provider if self._per_provider else "*"

    def wait_time(self, provider: str) -> float:
        """Giây phải chờ để được gọi ngay bây giờ (0 = đi luôn)."""
        now = self._clock.monotonic()
        with self._lock:
            w = self._windows.setdefault(self._key(provider), deque())
            while w and now - w[0] >= _WINDOW_S:
                w.popleft()
            if len(w) < self._cap:
                return 0.0
            return max(_WINDOW_S - (now - w[0]), 0.0)

    def acquire(self, provider: str, *, cancel: CancelToken) -> None:
        while True:
            delay = self.wait_time(provider)
            if delay <= 0:
                break
            if cancel.wait(delay):
                raise Cancelled(cancel.reason or "cancelled")
        with self._lock:
            self._windows.setdefault(self._key(provider), deque()).append(self._clock.monotonic())


class RetryBudget:
    """Đếm lỗi retryable liên tiếp cho một lượt chat."""

    def __init__(self, rate: RateCfg) -> None:
        self._rate = rate
        self.consecutive = 0
        self.attempts = 0

    @property
    def should_fallback(self) -> bool:
        return self.consecutive >= self._rate.on_429_consecutive

    def backoff_s(self) -> float:
        idx = min(self.consecutive - 1, len(self._rate.backoff) - 1)
        return float(self._rate.backoff[max(idx, 0)]) if self._rate.backoff else 1.0

    def record_failure(self) -> None:
        self.consecutive += 1
        self.attempts += 1

    def record_success(self) -> None:
        self.attempts += 1

    def reset_streak(self) -> None:
        self.consecutive = 0


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    budget: RetryBudget,
    cancel: CancelToken,
    on_retry: Callable[[ProviderError, float], None] | None = None,
) -> T:
    """Gọi fn tới khi thành công, hoặc raise lỗi retryable khi budget.should_fallback (caller đổi fallback)."""
    while True:
        cancel.raise_if_cancelled()
        try:
            out = fn()
        except ProviderError as e:
            if not e.retryable:
                raise
            budget.record_failure()
            if budget.should_fallback:
                raise
            delay = budget.backoff_s()
            if on_retry:
                on_retry(e, delay)
            if cancel.wait(delay):
                raise Cancelled(cancel.reason or "cancelled") from e
            continue
        budget.record_success()
        return out

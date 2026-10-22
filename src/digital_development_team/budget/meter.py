"""BudgetMeter (stage) · IssueMeter (issue). Đếm usage, check hard cap, báo mức cảnh báo vừa vượt."""

from __future__ import annotations

from collections.abc import Sequence

from digital_development_team.config.models import IssueBudget
from digital_development_team.core.context import BudgetRef, Clock, SystemClock
from digital_development_team.core.errors import BudgetExceeded
from digital_development_team.core.models import BudgetUsage

WARN_LEVELS: tuple[int, ...] = (60, 80, 90)


class BudgetMeter:
    scope = "stage"

    def __init__(
        self, ref: BudgetRef, clock: Clock | None = None, *, started_mono: float | None = None
    ) -> None:
        self.ref = ref
        self._clock = clock or SystemClock()
        self._start = self._clock.monotonic() if started_mono is None else started_mono
        self.usage = BudgetUsage()
        self._warned: set[int] = set()

    # ───── đo ─────
    @property
    def wall_s(self) -> float:
        return self._clock.monotonic() - self._start

    @property
    def tokens(self) -> int:
        return self.usage.tokens

    @property
    def turns(self) -> int:
        return self.usage.turns

    def add(self, usage: BudgetUsage) -> None:
        self.usage = self.usage + usage

    def add_turn(self, tokens_in: int, tokens_out: int, *, cached: int = 0, cost_usd: float = 0.0) -> None:
        self.add(
            BudgetUsage(
                turns=1,
                llm_calls=1,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=cached,
                cost_usd=cost_usd,
            )
        )

    # ───── ngưỡng ─────
    def pct(self) -> dict[str, float]:
        return {
            "turns": self.turns / self.ref.turns,
            "tokens": self.tokens / self.ref.tokens,
            "wall": self.wall_s / self.ref.wall_s,
        }

    def max_pct(self) -> float:
        return max(self.pct().values())

    def remaining_turns(self) -> int:
        return max(self.ref.turns - self.turns, 0)

    def remaining_tokens(self) -> int:
        return max(self.ref.tokens - self.tokens, 0)

    def remaining_wall_s(self) -> float:
        return max(self.ref.wall_s - self.wall_s, 0.0)

    def exceeded_axis(self) -> str | None:
        if self.turns >= self.ref.turns:
            return "turns"
        if self.tokens >= self.ref.tokens:
            return "tokens"
        if self.wall_s >= self.ref.wall_s:
            return "wall"
        return None

    def check(self) -> None:
        axis = self.exceeded_axis()
        if axis is not None:
            raise BudgetExceeded(
                f"{self.scope} budget hết ({axis}: {self._axis_text(axis)})",
                scope=self.scope,  # type: ignore[arg-type]
                axis=axis,
                hint="circuit breaker — không cắt, issue → BLOCKED",
            )

    def _axis_text(self, axis: str) -> str:
        if axis == "turns":
            return f"{self.turns}/{self.ref.turns}"
        if axis == "tokens":
            return f"{self.tokens}/{self.ref.tokens}"
        return f"{self.wall_s:.0f}s/{self.ref.wall_s:.0f}s"

    def crossed(self, levels: Sequence[int] = WARN_LEVELS) -> list[int]:
        """Mức % vừa vượt lần đầu (để emit budget_warning đúng 1 lần/mức)."""
        pct = self.max_pct() * 100
        new = [lv for lv in levels if pct >= lv and lv not in self._warned]
        self._warned.update(new)
        return new

    def snapshot(self) -> dict[str, int]:
        return {
            "tokens_used": self.tokens,
            "tokens_max": self.ref.tokens,
            "turns_used": self.turns,
            "turns_max": self.ref.turns,
            "wall_sec": int(self.wall_s),
            "wall_max_sec": int(self.ref.wall_s),
        }


class IssueMeter(BudgetMeter):
    """Cap toàn issue: tokens + wall (phút trong budgets.yaml). turns không giới hạn ở mức issue."""

    scope = "issue"

    def __init__(
        self, budget: IssueBudget, clock: Clock | None = None, *, usage: BudgetUsage | None = None
    ) -> None:
        super().__init__(BudgetRef(turns=10**9, wall_s=budget.wall * 60.0, tokens=budget.tokens), clock)
        if usage is not None:
            self.usage = usage
            # wall đã dùng từ checkpoint: dịch mốc bắt đầu lùi lại
            self._start -= usage.wall_s

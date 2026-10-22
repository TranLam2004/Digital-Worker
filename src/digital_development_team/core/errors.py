"""Cây lỗi. Nguyên tắc: lỗi agent tự sửa được → ToolResult(error, hint); lỗi harness → exception.

Exit code CLI: 0 done · 1 blocked · 2 config · 3 paused · 4 waiting gate.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal


class DDTError(Exception):
    code: ClassVar[str] = "DDT"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, hint: str | None = None, **ctx: Any) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.ctx = ctx

    def __str__(self) -> str:
        parts = [self.message]
        if self.hint:
            parts.append(f"→ {self.hint}")
        if self.ctx:
            parts.append(" ".join(f"{k}={v!r}" for k, v in self.ctx.items()))
        return " | ".join(parts)


# ───── Config ─────
class ConfigError(DDTError):
    code = "CONFIG"


class ExpressionError(ConfigError):
    code = "EXPR"


class MissingValue(DDTError):
    """Biểu thức tham chiếu field không tồn tại trong scope. Fail closed — không âm thầm thành None."""

    code = "MISSING_VALUE"


# ───── Schema / Store ─────
class SchemaViolation:
    __slots__ = ("message", "path")

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message

    def __repr__(self) -> str:
        return f"{self.path}: {self.message}"


class SchemaError(DDTError):
    code = "SCHEMA"

    def __init__(self, message: str, violations: list[SchemaViolation], **ctx: Any) -> None:
        super().__init__(message, hint="Sửa các field trên rồi ghi lại", **ctx)
        self.violations = violations

    def brief(self, limit: int = 5) -> list[dict[str, str]]:
        return [{"path": v.path, "msg": v.message} for v in self.violations[:limit]]


class StoreError(DDTError):
    code = "STORE"


class SlotPermissionError(StoreError):
    code = "SLOT_FORBIDDEN"


class IntegrityError(StoreError):
    code = "INTEGRITY"


class CheckpointError(DDTError):
    code = "CHECKPOINT"


# ───── Policy / Budget ─────
class PolicyDenied(DDTError):
    code = "POLICY_DENIED"

    def __init__(
        self, message: str, *, rule_id: str, layer: int, hint: str | None = None, **ctx: Any
    ) -> None:
        super().__init__(message, hint=hint, rule_id=rule_id, layer=layer, **ctx)
        self.rule_id = rule_id
        self.layer = layer


class BudgetExceeded(DDTError):
    code = "BUDGET"

    def __init__(
        self,
        message: str,
        *,
        scope: Literal["stage", "issue", "session"],
        axis: str,
        **ctx: Any,
    ) -> None:
        super().__init__(message, scope=scope, axis=axis, **ctx)
        self.scope = scope
        self.axis = axis


# ───── Tool / Sandbox ─────
class ToolFailure(DDTError):
    """Lỗi hạ tầng của tool. exit code ≠ 0 KHÔNG phải ToolFailure — đó là thông tin cho agent."""

    code = "TOOL"


class ToolTimeout(ToolFailure):
    code = "TIMEOUT"
    retryable = True


class SandboxError(DDTError):
    code = "SANDBOX"


# ───── Provider ─────
class ProviderError(DDTError):
    code = "PROVIDER"


class RateLimited(ProviderError):
    code = "RATE_LIMITED"
    retryable = True


class ProviderUnavailable(ProviderError):
    code = "PROVIDER_UNAVAILABLE"
    retryable = True


class ContextOverflow(ProviderError):
    code = "CONTEXT_OVERFLOW"


# ───── Isolation ─────
class IsolationError(DDTError):
    code = "ISOLATION"


class LockConflict(IsolationError):
    code = "LOCK_CONFLICT"

    def __init__(self, message: str, *, blocked_by: str, **ctx: Any) -> None:
        super().__init__(message, blocked_by=blocked_by, **ctx)
        self.blocked_by = blocked_by


class MergeConflict(IsolationError):
    code = "MERGE_CONFLICT"


# ───── Điều khiển ─────
class Cancelled(DDTError):
    """G3 / agent.stop. Dừng sạch, KHÔNG phải thất bại."""

    code = "CANCELLED"


class ProviderPaused(DDTError):
    """Provider tạm thời không dùng được (429 · hết hạn mức · unavailable) sau khi đã retry + fallback.
    Runner tạm dừng issue (paused, exit 3) thay vì BLOCKED — `ddt run` lần sau chạy lại stage hiện tại."""

    code = "PROVIDER_PAUSED"


class MatchDeferred(DDTError):
    """Contest action cannot proceed yet; preserve the checkpoint for retry."""

    code = "MATCH_DEFERRED"
    retryable = True


class GoldenExhausted(ProviderError):
    """MockProvider được gọi quá số lượt trong golden file."""

    code = "GOLDEN_EXHAUSTED"

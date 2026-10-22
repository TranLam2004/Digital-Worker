"""ModelRegistry — models.yaml → ModelRef theo role · adapter theo provider · LLMClient (retry/fallback/cost).

Resolve một role:
  1. ứng viên theo thứ tự: prefer (nếu alias có giá trị) → model → fallback
  2. loại ứng viên không khả dụng: provider tắt · thiếu API key · đang cooldown · adapter chưa hỗ trợ
  3. must_differ_family_from=X: ưu tiên ứng viên khác họ với model đã resolve của X; không có → lấy ứng viên đầu (warn ở config check)
  4. provider_override="mock" → mọi role về mock (dry-run/test)
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from digital_development_team.config.models import AliasCfg, ModelsConfig, ProviderCfg
from digital_development_team.core.context import CancelToken, Clock, SystemClock
from digital_development_team.core.errors import ConfigError, ProviderError, ProviderUnavailable
from digital_development_team.core.llm import ChatRequest, ChatResponse, ModelRef, Usage
from digital_development_team.core.protocols import LLMProvider
from digital_development_team.llm.pricing import cost_usd
from digital_development_team.llm.retry import RateLimiter, RetryBudget, call_with_retry

log = logging.getLogger(__name__)

MOCK_ALIAS = "mock"
LOOP_ROLES = frozenset(
    {"dev", "dev_rework", "dev_strong"}
)  # role loop dài → được ưu tiên provider agent_loop
CLI_SUFFIX = "_cli"
_UNSUPPORTED_ADAPTERS = {"gemini": "TODO(P2-#2b) adapter gemini", "bedrock": "TODO(P2-#2b) adapter bedrock"}
_MAX_RESOLVE_DEPTH = 3


@dataclass(frozen=True, slots=True)
class Resolution:
    role: str
    ref: ModelRef
    fallback: ModelRef | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatResult:
    response: ChatResponse
    ref: ModelRef
    cost_usd: float
    attempts: int
    fell_back: bool = False
    retries: tuple[str, ...] = field(default_factory=tuple)


class ModelRegistry:
    def __init__(
        self,
        cfg: ModelsConfig,
        *,
        provider_override: str | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        providers: Mapping[str, LLMProvider] | None = None,
        scratch: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._scratch = scratch or Path(tempfile.gettempdir()) / "ddt-llm"
        self._override = provider_override
        self._env = env if env is not None else os.environ
        self._clock = clock or SystemClock()
        self._providers: dict[str, LLMProvider] = dict(providers or {})
        self._lock = threading.Lock()
        self._cooldown_until: dict[str, float] = {}
        self._limiter = RateLimiter(cfg.rate.rpm_cap, clock=self._clock, per_provider=cfg.rate.per_provider)

    @property
    def config(self) -> ModelsConfig:
        return self._cfg

    def with_config(self, cfg: ModelsConfig) -> ModelRegistry:
        """Registry mới theo config mới (hot-reload giữa hai issue). Giữ provider đã dựng (mock, client có kết nối) và cooldown."""
        if cfg == self._cfg:
            return self
        nxt = ModelRegistry(
            cfg,
            provider_override=self._override,
            env=self._env,
            clock=self._clock,
            providers=self._providers,
            scratch=self._scratch,
        )
        nxt._cooldown_until = dict(self._cooldown_until)
        return nxt

    # ───── alias / khả dụng ─────
    def alias_ref(self, alias: str) -> ModelRef | None:
        a = self._cfg.aliases.get(alias)
        if a is None:
            return None
        return ModelRef(provider=a.provider, model=a.model, family=a.family, alias=alias, effort=a.effort)

    def has_key(self, provider: str) -> bool:
        p = self._cfg.providers.get(provider)
        if p is None:
            return False
        if provider in self._providers:  # provider tiêm sẵn (mock/test) không cần key
            return True
        if p.adapter == "claude_cli":  # transport CLI: "key" = binary trong PATH (đăng nhập subscription)
            from digital_development_team.llm.claude_cli_provider import ClaudeCliProvider

            return ClaudeCliProvider.binary_available(p)
        if p.adapter == "codex_cli":
            from digital_development_team.llm.codex_cli_provider import CodexCliProvider

            return CodexCliProvider.binary_available(p)
        return not p.api_key_env or bool(self._env.get(p.api_key_env))

    def in_cooldown(self, provider: str) -> bool:
        until = self._cooldown_until.get(provider)
        return until is not None and self._clock.monotonic() < until

    def mark_cooldown(self, provider: str) -> None:
        with self._lock:
            self._cooldown_until[provider] = self._clock.monotonic() + self._cfg.rate.fallback_cooldown_sec
        log.warning("provider %s vào cooldown %ss", provider, self._cfg.rate.fallback_cooldown_sec)

    def unavailable_reason(self, alias: str) -> str | None:
        """None = dùng được. Chuỗi = lý do không dùng được."""
        a: AliasCfg | None = self._cfg.aliases.get(alias)
        if a is None:
            return f"alias {alias!r} null/không có"
        p = self._cfg.providers.get(a.provider)
        if p is None:
            return f"provider {a.provider!r} không khai báo"
        if a.provider not in self._providers:
            if not p.enabled:
                return f"provider {a.provider} tắt"
            if p.adapter in _UNSUPPORTED_ADAPTERS:
                return _UNSUPPORTED_ADAPTERS[p.adapter]
        if not self.has_key(a.provider):
            return (
                f"thiếu ${p.api_key_env}"
                if not p.is_cli
                else f"không tìm thấy binary {p.binary or p.adapter.removesuffix('_cli')}"
            )
        if self.in_cooldown(a.provider):
            return f"provider {a.provider} đang cooldown"
        return None

    # ───── resolve ─────
    def resolve(self, role: str, *, _depth: int = 0) -> Resolution:
        rc = self._cfg.roles.get(role)
        if rc is None:
            raise ConfigError(
                f"role {role!r} không có trong models.yaml#roles", hint=f"có: {sorted(self._cfg.roles)}"
            )
        if self._override == MOCK_ALIAS:
            ref = self.alias_ref(MOCK_ALIAS)
            if ref is None:
                raise ConfigError("alias mock bị xoá khỏi models.yaml")
            return Resolution(role, ref, None, ("--provider mock",))

        candidates = [a for a in (rc.prefer, rc.model, rc.fallback) if a]
        notes: list[str] = []
        usable: list[ModelRef] = []
        for alias in candidates:
            why = self.unavailable_reason(alias)
            if why:
                notes.append(f"{alias}: {why}")
                continue
            ref = self.alias_ref(alias)
            if ref and ref not in usable:
                usable.append(ref)
        if not usable:
            # Quy tắc 8 (models.yaml): API tắt/thiếu key → thử alias song sinh transport CLI (<alias>_cli)
            for alias in candidates:
                twin = f"{alias}{CLI_SUFFIX}"
                if twin in self._cfg.aliases and self.unavailable_reason(twin) is None:
                    ref = self.alias_ref(twin)
                    if ref and ref not in usable:
                        usable.append(ref)
                        notes.append(f"{alias} → {twin} (transport CLI vì API không khả dụng)")
        if role not in LOOP_ROLES and len(usable) > 1:
            # role một call: ưu tiên adapter API trước provider agent_loop (CLI spawn process đắt hơn)
            usable.sort(key=lambda r: self._is_agent_loop(r.provider))
        if not usable:
            raise ConfigError(
                f"role {role}: không alias nào dùng được ({'; '.join(notes)})",
                hint="bật provider / đặt API key / điền alias trong models.yaml, hoặc --provider mock",
            )
        avoid_family: str | None = None
        if rc.must_differ_family_from and _depth < _MAX_RESOLVE_DEPTH:
            try:
                avoid_family = self.resolve(rc.must_differ_family_from, _depth=_depth + 1).ref.family
            except ConfigError as e:
                notes.append(f"không resolve được {rc.must_differ_family_from}: {e.message}")
        primary = usable[0]
        if avoid_family is not None:
            differ = [r for r in usable if r.family != avoid_family]
            if differ:
                primary = differ[0]
                if primary is not usable[0]:
                    notes.append(
                        f"chọn {primary.alias} để khác họ {rc.must_differ_family_from} ({avoid_family})"
                    )
            else:
                notes.append(
                    f"cùng họ với {rc.must_differ_family_from} ({avoid_family}) — không có alias khác họ"
                )
        fallback = next((r for r in usable if r != primary), None)
        return Resolution(role, primary, fallback, tuple(notes))

    def transport_of(self, provider: str) -> Literal["api", "cli"]:
        p = self._cfg.providers.get(provider)
        return "cli" if (p is not None and p.is_cli) else "api"

    def overhead_key_of(self, provider: str) -> str:
        """Khoá budgets.yaml#transport_overhead: api · cli (Claude Code) · codex (Codex CLI — đếm theo tool call)."""
        p = self._cfg.providers.get(provider)
        if p is None or not p.is_cli:
            return "api"
        return "codex" if p.adapter == "codex_cli" else "cli"

    def overhead_key_for(self, role: str) -> str:
        try:
            return self.overhead_key_of(self.resolve(role).ref.provider)
        except ConfigError:
            return "api"

    def transport_for(self, role: str) -> Literal["api", "cli"]:
        """Transport của provider mà role sẽ dùng (resolve lỗi → api — budget mặc định)."""
        try:
            return self.transport_of(self.resolve(role).ref.provider)
        except ConfigError:
            return "api"

    def _is_agent_loop(self, provider: str) -> bool:
        p = self._cfg.providers.get(provider)
        return bool(p and "agent_loop" in p.supports)

    # ───── provider ─────
    def provider(self, ref: ModelRef) -> LLMProvider:
        with self._lock:
            p = self._providers.get(ref.provider)
            if p is not None:
                return p
            cfg = self._cfg.providers.get(ref.provider)
            if cfg is None:
                raise ConfigError(f"provider {ref.provider!r} không khai báo")
            p = self._build(ref.provider, cfg)
            self._providers[ref.provider] = p
            return p

    def _build(self, name: str, cfg: ProviderCfg) -> LLMProvider:
        key = self._env.get(cfg.api_key_env) if cfg.api_key_env else None
        if cfg.adapter == "mock":
            from digital_development_team.llm.mock import MockProvider

            return MockProvider()
        if cfg.adapter == "anthropic":
            from digital_development_team.llm.anthropic_provider import AnthropicProvider

            return AnthropicProvider(cfg, api_key=key)
        if cfg.adapter in ("openai", "openai_compatible"):
            from digital_development_team.llm.openai_provider import OpenAICompatProvider

            return OpenAICompatProvider(name, cfg, api_key=key)
        if cfg.adapter == "claude_cli":
            from digital_development_team.llm.claude_cli_provider import ClaudeCliProvider

            return ClaudeCliProvider(cfg, scratch=self._scratch)
        if cfg.adapter == "codex_cli":
            from digital_development_team.llm.codex_cli_provider import CodexCliProvider

            return CodexCliProvider(cfg, scratch=self._scratch)
        raise ProviderUnavailable(
            f"adapter {cfg.adapter!r} chưa hỗ trợ", hint=_UNSUPPORTED_ADAPTERS.get(cfg.adapter)
        )

    def register_provider(self, name: str, provider: LLMProvider) -> None:
        with self._lock:
            self._providers[name] = provider

    # ───── client ─────
    def cost(self, ref: ModelRef, response: ChatResponse) -> float:
        return self.cost_of(ref, response.usage)

    def cost_of(self, ref: ModelRef, usage: Usage) -> float:
        over = self._cfg.pricing_by_provider.get(ref.provider)
        if over is not None:  # theo transport (CLI subscription = 0) thắng bảng giá theo model
            return cost_usd(ref.model, usage, {ref.model: over})
        return cost_usd(ref.model, usage, self._cfg.pricing)

    def client(self, role: str) -> LLMClient:
        return LLMClient(self, self.resolve(role))

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter


class LLMClient:
    """Một role → chat có retry/backoff, rate limit, fallback khi provider lỗi liên tiếp, tính cost."""

    def __init__(self, registry: ModelRegistry, resolution: Resolution) -> None:
        self._reg = registry
        self.resolution = resolution

    @property
    def ref(self) -> ModelRef:
        return self.resolution.ref

    @property
    def role(self) -> str:
        return self.resolution.role

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResult:
        rate = self._reg.config.rate
        ref = self.resolution.ref
        fell_back = False
        retries: list[str] = []
        budget = RetryBudget(rate)
        while True:
            provider = self._reg.provider(ref)
            if req.tools and not provider.caps.tool_use:
                raise ProviderError(
                    f"{provider.name} không hỗ trợ tool_use nhưng request có tool", role=self.role
                )
            current = ref
            request = req.model_copy(update={"model": current}) if req.model != current else req

            def _once(p: LLMProvider = provider, r: ChatRequest = request) -> ChatResponse:
                self._reg.limiter.acquire(p.name, cancel=cancel)
                return p.chat(r, cancel=cancel)

            try:
                resp = call_with_retry(
                    _once,
                    budget=budget,
                    cancel=cancel,
                    on_retry=lambda e, d: retries.append(f"{e.code} → chờ {d:g}s"),
                )
            except ProviderError as e:
                fb = self.resolution.fallback
                if e.retryable and fb is not None and not fell_back:
                    self._reg.mark_cooldown(current.provider)
                    retries.append(f"{e.code} x{budget.consecutive} → fallback {fb.alias}")
                    ref, fell_back = fb, True
                    budget.reset_streak()
                    continue
                raise
            return ChatResult(
                response=resp,
                ref=current,
                cost_usd=self._reg.cost(current, resp),
                attempts=budget.attempts,
                fell_back=fell_back,
                retries=tuple(retries),
            )

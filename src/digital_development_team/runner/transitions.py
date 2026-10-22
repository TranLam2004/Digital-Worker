"""TransitionTable — policy.yaml#transitions compile sẵn. Rule đầu khớp thắng; default (when=None) bắt buộc.

Thiếu giá trị trong biểu thức → MissingValue nổi lên (fail closed) → runner BLOCKED có lý do, không đoán.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from digital_development_team.config.models import PolicyConfig, TransitionRule
from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import ConfigError
from digital_development_team.expr.compiler import CompiledExpr, compile_expr


@dataclass(frozen=True, slots=True)
class Decision:
    to: tuple[Stage, ...]
    rule: TransitionRule
    index: int

    @property
    def primary(self) -> Stage:
        return self.to[0]

    @property
    def is_parallel(self) -> bool:
        return len(self.to) > 1

    @property
    def cancel(self) -> Stage | None:
        return Stage(self.rule.cancel) if self.rule.cancel else None

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {"to": "+".join(s.value for s in self.to), "rule": self.index}
        if self.rule.when:
            d["when"] = self.rule.when
        for k in ("rework", "reloc", "mg_retries", "cancel", "note"):
            v = getattr(self.rule, k)
            if v:
                d[k] = v
        if self.rule.bump:
            d["bump"] = sorted(self.rule.bump)
        if self.rule.skip:
            d["skip"] = True
        if self.rule.rule_only:
            d["rule_only"] = True
        return d


@dataclass(frozen=True, slots=True)
class _Compiled:
    rule: TransitionRule
    when: CompiledExpr | None
    to: tuple[Stage, ...]


class TransitionTable:
    def __init__(self, rules: Mapping[str, list[TransitionRule]]) -> None:
        self._table: dict[Stage, list[_Compiled]] = {}
        for stage_name, stage_rules in rules.items():
            try:
                stage = Stage(stage_name)
            except ValueError:
                raise ConfigError(f"transitions: stage lạ {stage_name!r}") from None
            compiled: list[_Compiled] = []
            for r in stage_rules:
                tos = [r.to] if isinstance(r.to, str) else list(r.to)
                try:
                    to = tuple(Stage(t) for t in tos)
                except ValueError as e:
                    raise ConfigError(f"transitions.{stage_name}: đích lạ {tos}") from e
                compiled.append(_Compiled(r, compile_expr(r.when) if r.when else None, to))
            if not any(c.when is None for c in compiled):
                raise ConfigError(f"transitions.{stage_name}: thiếu rule default")
            self._table[stage] = compiled

    @classmethod
    def from_policy(cls, policy: PolicyConfig) -> TransitionTable:
        return cls(policy.transitions)

    def has(self, stage: Stage) -> bool:
        return stage in self._table

    def next(self, stage: Stage, scope: Mapping[str, Any]) -> Decision:
        rules = self._table.get(stage)
        if rules is None:
            raise ConfigError(f"transitions: không có bảng cho stage {stage.value}")
        for i, c in enumerate(rules):
            if c.when is None or c.when.as_bool(scope):
                return Decision(c.to, c.rule, i)
        raise ConfigError(
            f"transitions.{stage.value}: không rule nào khớp"
        )  # pragma: no cover - default bắt buộc

    def peek_skip(self, stage: Stage, scope: Mapping[str, Any]) -> bool:
        """Rule khớp đầu tiên có skip=True → stage này được bỏ qua (REVIEW cho T0)."""
        return self.next(stage, scope).rule.skip

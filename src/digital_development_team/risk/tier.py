"""Risk tier từ policy.yaml#tier_rules (70-human-control/01-risk-tiers.md).

Ngữ nghĩa:
- Mọi rule đều được đánh giá; kết quả = MAX các tier tuyệt đối khớp (T0..T3, $value).
- "+1"/"+2" cộng vào MAX đó (kẹt trần T3).
- Rule thiếu dữ liệu (MissingValue) → bỏ qua và ghi vào `skipped`: dữ liệu chưa có ở stage này
  (vd confidence chỉ có sau VERIFY). Tier được tính lại sau MỖI stage nên rule sẽ bắt kịp.
  Không fail closed ở đây vì fail closed = T3 cho mọi issue ngay từ TRIAGE.
- Không bao giờ hạ: kết quả cuối = max(current, computed).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from digital_development_team.config.models import TierRule
from digital_development_team.core.enums import Tier
from digital_development_team.core.errors import ConfigError, MissingValue
from digital_development_team.expr.compiler import CompiledExpr, compile_expr

_HAS_RE = re.compile(r"has\(\s*([a-z_]+)\s*,\s*'([a-z_]+)'\s*\)")


@dataclass(frozen=True, slots=True)
class TierResult:
    tier: Tier
    reasons: tuple[str, ...]  # rule khớp, theo thứ tự
    skipped: tuple[str, ...] = field(default_factory=tuple)  # rule thiếu dữ liệu

    @property
    def bumped_because(self) -> list[str]:
        return list(self.reasons)


@dataclass(frozen=True, slots=True)
class _Rule:
    rule: TierRule
    when: CompiledExpr | None
    value_path: tuple[str, str] | None  # cho "$value": (root, field) lấy từ has(root, 'field')


def _compile(rules: Sequence[TierRule]) -> list[_Rule]:
    out: list[_Rule] = []
    for r in rules:
        when = compile_expr(r.when) if r.when else None
        vp: tuple[str, str] | None = None
        if r.tier == "$value":
            m = _HAS_RE.search(r.when or "")
            if not m:
                raise ConfigError(f"tier_rules: '$value' cần when dạng has(x, 'field'): {r.when!r}")
            vp = (m.group(1), m.group(2))
        out.append(_Rule(r, when, vp))
    return out


_CACHE: dict[int, list[_Rule]] = {}


def compute_tier(
    rules: Sequence[TierRule], scope: Mapping[str, Any], *, current: Tier | None = None
) -> TierResult:
    key = id(rules)
    compiled = _CACHE.get(key)
    if compiled is None:
        compiled = _CACHE[key] = _compile(rules)

    absolute: list[Tier] = []
    default_tier = Tier.T1
    bumps = 0
    reasons: list[str] = []
    skipped: list[str] = []
    for c in compiled:
        try:
            if c.when is not None and not c.when.as_bool(scope):
                continue
            if c.value_path is not None:
                root, fld = c.value_path
                val = scope[root][fld]
                t = Tier(str(val))
                absolute.append(t)
                reasons.append(f"{root}.{fld}={t.value}")
                continue
        except MissingValue:
            skipped.append(c.rule.when or "<default>")
            continue
        except (KeyError, ValueError) as e:
            skipped.append(f"{c.rule.when}: {e}")
            continue
        if c.rule.tier.startswith("+"):
            bumps += int(c.rule.tier[1:])
            reasons.append(f"{c.rule.when} → {c.rule.tier}")
        elif c.when is None:
            default_tier = Tier(c.rule.tier)  # chỉ dùng khi không rule tuyệt đối nào khớp
        else:
            t = Tier(c.rule.tier)
            absolute.append(t)
            reasons.append(f"{c.rule.when} → {t.value}")
    base = Tier.max_of(*absolute) if absolute else default_tier
    computed = base.bump(bumps) if bumps else base
    final = Tier.max_of(current, computed)
    return TierResult(final, tuple(reasons), tuple(skipped))

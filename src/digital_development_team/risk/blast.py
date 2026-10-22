"""Blast radius → FMEA (Severity · Occurrence · Detection → RPN) — hàm thuần, 0 I/O, test được bằng số.

Ý tưởng mượn từ Honda POC (cùng engine CodeGraph): S theo số file ảnh hưởng (+ public API/sensitive), O theo độ sâu lan
và số symbol đổi, D theo coverage dòng mới (không có coverage → 7 = "không biết"). RPN = S·O·D; ngưỡng trong
policy.yaml#blast_radius (mặc định high ≥ 200, medium ≥ 80). Chỉ là tín hiệu cho Leader/confidence — không tự bump tier
giữa issue (tier khóa sau TRIAGE).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class Fmea:
    severity: int
    occurrence: int
    detection: int
    rpn: int
    risk: Risk

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "occurrence": self.occurrence,
            "detection": self.detection,
            "rpn": self.rpn,
            "risk": self.risk,
        }


def _clamp(v: float, lo: int = 1, hi: int = 10) -> int:
    return int(min(max(round(v), lo), hi))


def fmea(
    *,
    affected_files: int,
    depth_reached: int,
    changed_symbols: int,
    coverage_changed_pct: float | None,
    touches_public_api: bool = False,
    touches_sensitive: bool = False,
    rpn_high: int = 200,
    rpn_medium: int = 80,
) -> Fmea:
    """S = 1 + ceil(files/5) (+2 public API, +2 sensitive) · O = 1 + depth + ceil(symbols/5) ·
    D = 1 + 9·(1 − coverage_dòng_mới) ; không có coverage → 7. Mỗi trục kẹp [1, 10]."""
    s = 1 + math.ceil(affected_files / 5) + (2 if touches_public_api else 0) + (2 if touches_sensitive else 0)
    o = 1 + max(depth_reached, 0) + math.ceil(changed_symbols / 5)
    if coverage_changed_pct is None:
        d = 7.0
    else:
        d = 1 + 9 * (1 - min(max(coverage_changed_pct, 0.0), 100.0) / 100.0)
    sev, occ, det = _clamp(s), _clamp(o), _clamp(d)
    rpn = sev * occ * det
    risk: Risk = "high" if rpn >= rpn_high else ("medium" if rpn >= rpn_medium else "low")
    return Fmea(sev, occ, det, rpn, risk)


def impact_risk(
    affected_files: int, *, touches_public_api: bool = False, low: int = 3, medium: int = 8
) -> Risk:
    """Rủi ro theo contract codegraph.impact (40-tools/codegraph.tool.md): low < 3 file · medium 3–8 · high > 8 hoặc public API."""
    if touches_public_api or affected_files > medium:
        return "high"
    if affected_files >= low:
        return "medium"
    return "low"

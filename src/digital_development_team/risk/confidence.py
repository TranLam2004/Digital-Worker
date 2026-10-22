"""Confidence từ 5 tín hiệu đo được (70-human-control/03). Harness tính, 0 LLM, max 0.90 (cố ý).

confidence = 0.30·test_signal + 0.25·verifier_agreement + 0.20·spec_clarity + 0.15·familiarity − 0.20·rework_penalty
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from digital_development_team.config.models import ConfidenceWeights

UNKNOWN_FAMILIARITY = 0.4  # module chưa có worker_stats (P3 memory) → 0.4 theo thiết kế


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    value: float
    breakdown: dict[str, float]
    weakest: str
    notes: tuple[str, ...] = ()

    def to_facts(self) -> dict[str, Any]:
        return {
            "confidence": self.value,
            "confidence_breakdown": self.breakdown,
            "confidence_weakest": self.weakest,
        }


def test_signal(evidence: Mapping[str, Any]) -> float:
    criteria = [c for c in evidence.get("criteria", []) if isinstance(c, Mapping)]
    tests = [
        c
        for c in criteria
        if c.get("evidence_type") == "test_output" or str(c.get("id", "")).startswith("AC")
    ]
    if not criteria:
        return 0.0
    passed = sum(1 for c in criteria if c.get("result") == "pass")
    base = passed / len(criteria)
    reg = evidence.get("regression") or {}
    if reg.get("result") != "pass":
        base *= 0.5
    mg = evidence.get("machine_gate") or {}
    cov = mg.get("coverage_delta")
    if isinstance(cov, (int, float)) and cov < -2:
        base *= 0.8
    br = mg.get("blast_radius")
    if isinstance(br, Mapping) and br.get("risk") == "high":
        base *= 0.9  # lan rộng mà test chỉ chứng minh AC → bớt tin
    _ = tests
    return round(min(max(base, 0.0), 1.0), 3)


def verifier_agreement(evidence: Mapping[str, Any], sec_report: Mapping[str, Any] | None) -> float:
    v = evidence.get("verdict")
    s = sec_report.get("verdict") if sec_report else None
    if v == "fail" or s == "fail":
        return 0.0
    if v == "inconclusive" or s == "needs_human":
        return 0.5
    if v == "pass" and s == "pass":
        return 1.0
    if v == "pass" and s is None:
        return 0.9  # security skipped (T0 regex sạch)
    return 0.5


def spec_clarity(spec: Mapping[str, Any], evidence: Mapping[str, Any]) -> float:
    amb = len(spec.get("ambiguities") or [])
    criteria = evidence.get("criteria") or []
    unclear = sum(1 for c in criteria if isinstance(c, Mapping) and c.get("result") == "unclear")
    total = max(len(criteria), 1)
    return round(max(1.0 - 0.2 * amb - 0.3 * (unclear / total), 0.0), 3)


def rework_penalty(rework: int, reloc: int) -> float:
    n = rework + reloc
    return 0.0 if n == 0 else (0.5 if n == 1 else 1.0)


def compute_confidence(
    weights: ConfidenceWeights,
    *,
    evidence: Mapping[str, Any],
    spec: Mapping[str, Any],
    sec_report: Mapping[str, Any] | None,
    rework: int,
    reloc: int,
    familiarity: float = UNKNOWN_FAMILIARITY,
) -> ConfidenceResult:
    br = {
        "test_signal": test_signal(evidence),
        "verifier_agreement": verifier_agreement(evidence, sec_report),
        "spec_clarity": spec_clarity(spec, evidence),
        "familiarity": round(min(max(familiarity, 0.0), 1.0), 3),
        "rework_penalty": rework_penalty(rework, reloc),
    }
    value = (
        weights.test_signal * br["test_signal"]
        + weights.verifier_agreement * br["verifier_agreement"]
        + weights.spec_clarity * br["spec_clarity"]
        + weights.familiarity * br["familiarity"]
        - weights.rework_penalty * br["rework_penalty"]
    )
    value = round(min(max(value, 0.0), 1.0), 3)
    positives = {k: v for k, v in br.items() if k != "rework_penalty"}
    weakest = min(positives, key=lambda k: positives[k])
    if br["rework_penalty"] >= 0.5:  # rework là tín hiệu hành động được → câu hỏi escalate theo rework
        weakest = "rework_penalty"
    notes = []
    if sec_report is None:
        notes.append("security skipped")
    if familiarity == UNKNOWN_FAMILIARITY:
        notes.append("familiarity mặc định 0.4 (chưa có worker_stats)")
    return ConfidenceResult(value, br, weakest, tuple(notes))

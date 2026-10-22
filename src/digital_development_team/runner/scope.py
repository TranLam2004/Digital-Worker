"""Scope cho biểu thức policy (transitions · tier_rules · gates). Lười: slot chỉ đọc khi rule đụng tới.

Tên trong scope (policy.yaml ghi chú):
  intake · plan · spec · evidence · sec_report · decision   ← slot mới nhất, ưu tiên hơn facts cùng tên (thiếu cả hai → MissingValue)
  patch                                                     ← PatchBundle dict
  machine_gate · checklist · merge · gate · outcome · confidence · … ← facts do stage trả
  files_touched (từ patch, [] nếu chưa có) · sensitive_paths (globs)
  state{tier, rework, reloc, mg_retries, rejections, attempt, g0_visits, touches_sensitive, <counters>}
  limits{max_rework, max_reloc, machine_gate_free_retries, conf_proceed, conf_bump_lo, conf_bump_hi, conf_escalate}
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from digital_development_team.blackboard.slots import SLOTS
from digital_development_team.blackboard.store import FileStore
from digital_development_team.config.loader import ResolvedConfig
from digital_development_team.core.errors import MissingValue, StoreError
from digital_development_team.expr.compiler import compile_expr
from digital_development_team.runner.state import PARALLEL_KEY, IssueState

SLOT_BY_NAME: dict[str, str] = {
    "item": "item.json",
    "intake": "intake.yaml",
    "plan": "plan.yaml",
    "spec": "spec.yaml",
    "patch": "patch/",
    "evidence": "evidence.json",
    "sec_report": "sec_report.json",
    "decision": "decision.json",
    "escalation": "escalation.yaml",
    "clarify": "clarify.yaml",
    "answer": "answer.yaml",
}


def limits_for(cfg: ResolvedConfig, state: IssueState) -> dict[str, Any]:
    p = cfg.policy
    lo, hi = p.effective_conf_bump_range()
    return {
        "max_rework": p.max_rework[state.tier.value],
        "max_reloc": p.max_reloc[state.tier.value],
        "machine_gate_free_retries": p.machine_gate_free_retries,
        "slot_write_attempts": p.slot_write_attempts,
        "conf_proceed": p.effective_conf_proceed(),
        "conf_bump_lo": lo,
        "conf_bump_hi": hi,
        "conf_escalate": p.conf_escalate,
    }


COUNTERS = ("verify_retries", "respec")
"""Bộ đếm chung (transition `bump:`) luôn có mặt với giá trị 0 → luật so sánh không bao giờ thiếu giá trị."""


def state_view(state: IssueState) -> dict[str, Any]:
    raw = state.facts.get("counters")
    counters = {k: int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    return {
        **{name: 0 for name in COUNTERS},
        **counters,
        "tier": state.tier.value,
        "rework": state.rework,
        "reloc": state.reloc,
        "mg_retries": state.mg_retries,
        "rejections": state.rejections,
        "attempt": state.attempt,
        "g0_visits": state.g0_visits(),
        "touches_sensitive": bool(state.facts.get("touches_sensitive", False)),
        "stage": state.stage.value,
    }


class LazyScope(Mapping[str, Any]):
    """Mapping: key có → giá trị; key là slot → đọc store (StoreError → coi như thiếu → KeyError → MissingValue)."""

    def __init__(self, state: IssueState, store: FileStore, cfg: ResolvedConfig) -> None:
        self._state = state
        self._store = store
        self._issue = state.issue_id
        self._cfg = cfg
        self._cache: dict[str, Any] = {}
        self._static: dict[str, Any] = {
            k: v for k, v in state.facts.items() if k != PARALLEL_KEY and not k.startswith("_")
        }
        self._static["state"] = state_view(state)
        self._static["limits"] = limits_for(cfg, state)
        self._static["sensitive_paths"] = list(cfg.sensitive_paths.globs())
        self._static["policy"] = {
            "autonomy": cfg.policy.autonomy,
            "wip_limit": cfg.policy.wip_limit,
            "review_skip_tiers": list(cfg.policy.review_skip_tiers),
            "split_enabled": cfg.policy.split_enabled,
        }

    def _slot(self, name: str) -> Any:
        if name in self._cache:
            return self._cache[name]
        slot = SLOT_BY_NAME[name]
        try:
            content = self._store.read(self._issue, slot).content
        except StoreError:
            raise KeyError(name) from None
        self._cache[name] = content
        return content

    def __getitem__(self, key: str) -> Any:
        # tên slot → artifact thật trước. facts chỉ giữ bản tóm tắt kiểu đơn (bỏ list/dict), nếu đọc facts trước
        # thì luật như len(spec.ambiguities) luôn thấy rỗng. Slot chưa ghi → rơi về facts.
        if key in SLOT_BY_NAME:
            try:
                return self._slot(key)
            except KeyError:
                pass
        if key in self._static:
            return self._static[key]
        if key == "g1_required":
            return self._g1_required()
        if key == "files_touched":
            try:
                return list(self._slot("patch").get("files_touched", []))
            except KeyError:
                return []
        raise KeyError(key)

    def _g1_required(self) -> bool:
        """OR của policy.gates.g1.requires_human_when trên chính scope này. Rule thiếu dữ liệu → bỏ qua."""
        for expr in self._cfg.policy.gates.g1.requires_human_when:
            try:
                if compile_expr(expr).as_bool(self):
                    return True
            except MissingValue:
                continue
        return False

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        if key in SLOT_BY_NAME:
            try:
                self._slot(key)
                return True
            except KeyError:
                pass
        return key in self._static or key in ("files_touched", "g1_required")

    def __iter__(self) -> Iterator[str]:
        keys = set(self._static) | {"files_touched", "g1_required"}
        for name, slot in SLOT_BY_NAME.items():
            if slot in SLOTS and self._store.exists(self._issue, slot):
                keys.add(name)
        return iter(sorted(keys))

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def snapshot(self) -> dict[str, Any]:
        """Dump để debug/replay (đọc hết slot có)."""
        return {k: self[k] for k in self}

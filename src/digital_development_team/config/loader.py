"""Nạp 4 file config qua AssetResolver → Pydantic → dotted patches (preset, flags) → ResolvedConfig frozen.

Quy tắc ghép: file YAML ở lớp trên THAY NGUYÊN FILE. Chỉnh nhỏ → dotted patch.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from digital_development_team.config.models import (
    BudgetsConfig,
    ModelsConfig,
    PolicyConfig,
    SensitivePathsConfig,
)
from digital_development_team.config.resources import AssetResolver, build_resolver
from digital_development_team.core.errors import ConfigError
from digital_development_team.util import yamlio
from digital_development_team.util.hashing import sha256_json

FILES = {
    "policy": "config/policy.yaml",
    "budgets": "config/budgets.yaml",
    "models": "config/models.yaml",
    "sensitive_paths": "config/sensitive_paths.yaml",
}

# CLI flag → dotted patch (file, key, value)
FLAG_PATCHES: dict[str, list[tuple[str, str, Any]]] = {
    "--minimal": [("policy", "minimal", True), ("policy", "no_learn", True)],
    "--no-learn": [("policy", "no_learn", True)],
    "--no-transcripts": [("policy", "transcripts", False)],
    "--conservative": [("policy", "autonomy", "conservative")],
    "--economy": [("policy", "economy_mode", True)],
    "--no-canary": [("policy", "canary.enabled", False)],
    "--no-delegate": [("policy", "delegation.enabled", False)],
    "--no-clarify": [("policy", "clarify.enabled", False)],
    "--no-peer": [("policy", "rituals.peer_review_tiers", [])],
    "--sweep": [("policy", "sweep_mode", True)],
    "--sequential": [("policy", "wip_limit", 1)],
    # thi đấu: không chờ người, không lượt LLM không ra điểm (Learner, canary), Leader chỉ khi T2+/có tín hiệu xấu
    "--match": [
        ("policy", "autonomy", "autonomous"),
        ("policy", "no_learn", True),
        ("policy", "canary.enabled", False),
        ("policy", "intake_consistency.enabled", False),
        ("policy", "gate_rule_tiers", ["T0", "T1"]),
        ("policy", "review_skip_tiers", ["T0", "T1"]),
        ("policy", "split_enabled", False),
        # tool không ra điểm trong trận: stub (agent.*, user.ask), mạng/cài gói, hỏi LLM phụ, memory (repo mới, Learner tắt)
        (
            "policy",
            "tool_denylist",
            [
                "agent.*",
                "user.ask",
                "pkg.*",
                "web.*",
                "docs.lookup",
                "llm.ask",
                "skill.search",
                "memory.*",
                "mcp.*",
            ],
        ),
    ],
}


def apply_dotted(d: dict[str, Any], dotted: str, value: Any) -> None:
    """apply_dotted(cfg, 'canary.enabled', False). Tạo dict trung gian nếu thiếu."""
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    policy: PolicyConfig
    budgets: BudgetsConfig
    models: ModelsConfig
    sensitive_paths: SensitivePathsConfig
    sources: Mapping[str, str]  # file → layer
    patches: tuple[str, ...]  # mô tả patch đã áp
    fingerprint: str  # sha256 của toàn bộ raw đã patch
    preset: str | None = (
        None  # preset truyền vào (không gồm runtime.yaml) — process con (ddt mcp) nạp lại đúng như vậy
    )
    flags: tuple[str, ...] = ()  # flags truyền vào (không gồm runtime.yaml)

    def summary(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "sources": dict(self.sources),
            "patches": list(self.patches),
            "wip_limit": self.policy.wip_limit,
            "autonomy": self.policy.autonomy,
            "minimal": self.policy.minimal,
            "sandbox": self.policy.sandbox.kind,
            "providers_enabled": [k for k, v in self.models.providers.items() if v.enabled],
        }


def _load_raw(resolver: AssetResolver, rel: str) -> dict[str, Any]:
    data = yamlio.loads(resolver.read_text(rel), source=rel)
    if not isinstance(data, dict):
        raise ConfigError(f"{rel}: gốc phải là mapping")
    return data


def _validate[M](cls: type[M], raw: dict[str, Any], rel: str) -> M:
    try:
        return cls.model_validate(raw)  # type: ignore[attr-defined, no-any-return]
    except ValidationError as e:
        lines = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:5])
        raise ConfigError(f"{rel} không hợp lệ: {lines}", hint="xem config/models.py") from e


def load_config(
    *,
    home: Path | None = None,
    config_dir: Path | None = None,
    flags: Sequence[str] = (),
    preset: str | None = None,
    overrides: Sequence[tuple[str, str, Any]] = (),
    resolver: AssetResolver | None = None,
) -> ResolvedConfig:
    """flags: ['--minimal', '--wip', '2']. overrides: [('policy', 'wip_limit', 2)]."""
    res = resolver or build_resolver(home=home, config_dir=config_dir)
    preset_arg, flags_arg = preset, tuple(flags)
    raw = {name: _load_raw(res, rel) for name, rel in FILES.items()}
    applied: list[str] = []
    # 0. runtime patch (<home>/overrides/config/runtime.yaml): preset/flags mặc định + patch dotted ở bước 3b
    from digital_development_team.config.runtime_patch import flatten, read_runtime

    runtime = read_runtime(home)
    if preset is None and runtime.get("preset"):
        preset = str(runtime["preset"])
        applied.append(f"runtime:preset:{preset}")
    rt_flags = runtime.get("flags") or []
    if rt_flags:
        flags = [*flags, *(str(f) for f in rt_flags)]

    # 1. preset từ models.yaml#presets
    if preset:
        presets = raw["models"].get("presets", {})
        if preset not in presets:
            raise ConfigError(f"preset {preset!r} không có", hint=f"có: {sorted(presets)}")
        for dotted, value in presets[preset].items():
            if dotted == "note":
                continue
            apply_dotted(raw["models"], dotted, copy.deepcopy(value))
            applied.append(f"preset:{preset}:{dotted}")

    # 2. flags
    i = 0
    flags = list(flags)
    while i < len(flags):
        f = flags[i]
        if f == "--wip":
            if i + 1 >= len(flags):
                raise ConfigError("--wip cần số")
            apply_dotted(raw["policy"], "wip_limit", int(flags[i + 1]))
            applied.append(f"flag:--wip={flags[i + 1]}")
            i += 2
            continue
        if f in FLAG_PATCHES:
            for file, dotted, value in FLAG_PATCHES[f]:
                apply_dotted(raw[file], dotted, value)
            applied.append(f"flag:{f}")
        elif f.startswith("--"):
            raise ConfigError(f"flag lạ: {f}", hint=f"có: {sorted(FLAG_PATCHES)} --wip N")
        i += 1

    # 3. overrides tường minh
    for file, dotted, value in overrides:
        if file not in raw:
            raise ConfigError(f"override file lạ: {file}")
        apply_dotted(raw[file], dotted, value)
        applied.append(f"override:{file}:{dotted}")

    # 3b. runtime patch dotted theo file (web Settings · ddt runtime set)
    for file in FILES:
        for dotted, value in flatten(runtime.get(file) or {}):
            apply_dotted(raw[file], dotted, copy.deepcopy(value))
            applied.append(f"runtime:{file}:{dotted}")

    # 4. economy mode → áp preset economy của models + budgets.economy
    policy_raw = raw["policy"]
    if policy_raw.get("economy_mode"):
        eco = raw["models"].get("presets", {}).get("economy", {})
        for dotted, value in eco.items():
            if dotted != "note":
                apply_dotted(raw["models"], dotted, copy.deepcopy(value))
        for k, v in raw["budgets"].get("economy", {}).items():
            if k == "stages":
                for stage, tiers in v.items():
                    for tier, b in tiers.items():
                        apply_dotted(raw["budgets"], f"stages.{stage}.{tier}", b)
            elif k in ("wip_limit",):
                apply_dotted(policy_raw, "wip_limit", v)
            elif k == "memory_block_tokens":
                apply_dotted(policy_raw, "condenser_defaults.memory_block_tokens", v)
            elif k == "canary":
                apply_dotted(policy_raw, "canary.enabled", bool(v))
        applied.append("economy_mode")

    policy = _validate(PolicyConfig, raw["policy"], FILES["policy"])
    budgets = _validate(BudgetsConfig, raw["budgets"], FILES["budgets"])
    models = _validate(ModelsConfig, raw["models"], FILES["models"])
    sensitive = _validate(SensitivePathsConfig, raw["sensitive_paths"], FILES["sensitive_paths"])

    return ResolvedConfig(
        policy=policy,
        budgets=budgets,
        models=models,
        sensitive_paths=sensitive,
        sources=res.sources(list(FILES.values())),
        patches=tuple(applied),
        fingerprint=sha256_json(raw),
        preset=preset_arg,
        flags=flags_arg,
    )

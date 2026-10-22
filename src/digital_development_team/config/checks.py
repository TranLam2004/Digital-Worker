"""`ddt config check`: mọi lỗi config phải lộ ở đây, không lúc chạy.

- compile mọi expression (transitions, tier_rules, gates)
- budget: issue cap ≥ Σ stage ceiling + 1 rework cho mọi tier
- resolve alias cho mọi role; provider enabled; must_differ_family
- glob compile được
- transitions: mọi stage có default; `to` là Stage hợp lệ
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from digital_development_team.config.loader import ResolvedConfig
from digital_development_team.core.enums import Stage, Tier
from digital_development_team.core.errors import ExpressionError
from digital_development_team.expr import compile_expr
from digital_development_team.util.globs import GlobSet

PIPELINE_STAGES = ("TRIAGE", "LOCALIZE", "SPEC", "IMPLEMENT", "VERIFY", "REVIEW", "GATE")


@dataclass
class CheckReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.notes.append(msg)


def run_checks(cfg: ResolvedConfig) -> CheckReport:
    r = CheckReport()
    _check_expressions(cfg, r)
    _check_transitions(cfg, r)
    _check_budgets(cfg, r)
    _check_models(cfg, r)
    _check_globs(cfg, r)
    _check_misc(cfg, r)
    _check_tooling(r)
    return r


# binary → slot/tool dùng nó. Thiếu → cảnh báo (slot skipped), không phải lỗi.
OPTIONAL_TOOLING: dict[str, str] = {
    "bandit": "security.scan sast · machine gate sast",
    "pip-audit": "security.scan deps",
    "detect-secrets": "security.scan secrets",
    "diff-cover": "test.coverage · machine gate coverage",
    "codegraph": "codegraph.* · blast radius (thiếu → fallback PyGraph thuần Python, chỉ hỗ trợ .py)",
}


def _check_tooling(r: CheckReport) -> None:
    missing = [name for name in OPTIONAL_TOOLING if shutil.which(name) is None]
    for name in missing:
        r.warn(
            f"thiếu binary {name!r} → {OPTIONAL_TOOLING[name]} sẽ skipped — scanner/coverage là dependency chính (pip install -e .), chỉ codegraph là npm (npm i -g @colbymchenry/codegraph; thiếu → fallback PyGraph ast cho Python)"
        )
    if not missing:
        r.info("tooling: " + ", ".join(OPTIONAL_TOOLING) + " đều có")


def _try_compile(src: str, where: str, r: CheckReport) -> None:
    try:
        compile_expr(src)
    except ExpressionError as e:
        r.err(f"{where}: {e.message} — `{src}`")


def _check_expressions(cfg: ResolvedConfig, r: CheckReport) -> None:
    p = cfg.policy
    for stage, rules in p.transitions.items():
        for i, rule in enumerate(rules):
            if rule.when:
                _try_compile(rule.when, f"transitions.{stage}[{i}].when", r)
    for i, trule in enumerate(p.tier_rules):
        if trule.when:
            _try_compile(trule.when, f"tier_rules[{i}].when", r)
    for gname in ("g0", "g1", "g2", "g3"):
        g = getattr(p.gates, gname)
        for i, src in enumerate(g.trigger):
            _try_compile(src, f"gates.{gname}.trigger[{i}]", r)
        for i, src in enumerate(g.requires_human_when):
            _try_compile(src, f"gates.{gname}.requires_human_when[{i}]", r)


def _check_transitions(cfg: ResolvedConfig, r: CheckReport) -> None:
    valid = {s.value for s in Stage}
    for stage, rules in cfg.policy.transitions.items():
        if stage not in valid:
            r.err(f"transitions: stage lạ {stage!r}")
        if not any(rule.when is None for rule in rules):
            r.err(f"transitions.{stage}: thiếu rule default (when: null)")
        for i, rule in enumerate(rules):
            targets = rule.to if isinstance(rule.to, list) else [rule.to]
            for t in targets:
                if t not in valid:
                    r.err(f"transitions.{stage}[{i}].to: stage lạ {t!r}")
    for s in PIPELINE_STAGES:
        if s not in cfg.policy.transitions and s not in ("LOCALIZE", "IMPLEMENT", "REVIEW"):
            r.warn(f"transitions: không có bảng cho {s} (dùng mặc định tuyến tính)")


def _check_budgets(cfg: ResolvedConfig, r: CheckReport) -> None:
    b = cfg.budgets
    for tier in Tier:
        try:
            one_pass = sum(b.stages[s].for_tier(tier).tokens for s in PIPELINE_STAGES if s in b.stages)
            rework = b.stages["IMPLEMENT"].for_tier(tier).tokens + b.stages["VERIFY"].for_tier(tier).tokens
        except (KeyError, ValueError) as e:
            r.err(f"budgets.stages thiếu cho {tier}: {e}")
            continue
        cap = b.issue[tier.value].tokens
        if cap < one_pass + rework:
            r.err(
                f"budgets.issue.{tier}.tokens={cap:,} < 1 pass {one_pass:,} + 1 rework {rework:,} = {one_pass + rework:,} — retry policy vô nghĩa"
            )
        else:
            r.info(
                f"budget {tier}: pass {one_pass // 1000}k + rework {rework // 1000}k ≤ cap {cap // 1000}k ✓"
            )
    if cfg.policy.conf_proceed > cfg.policy.confidence_weights.max_possible:
        r.err(
            f"conf_proceed {cfg.policy.conf_proceed} > max possible {cfg.policy.confidence_weights.max_possible:.2f}"
        )


def _check_models(cfg: ResolvedConfig, r: CheckReport) -> None:
    m = cfg.models
    for pname, pcfg in m.providers.items():
        if pcfg.adapter == "claude_cli" and pcfg.enabled:
            head = (pcfg.binary or "claude").split(" ", 1)[0]
            if shutil.which(head) is None and not Path(head).exists():
                r.err(f"provider {pname}: không tìm thấy binary {head!r} (Claude Code CLI) trong PATH")
            else:
                r.info(f"provider {pname}: transport CLI qua {head} — mọi tool qua MCP DDT, cost_usd=0")
        if pcfg.adapter == "codex_cli" and pcfg.enabled:
            head = (pcfg.binary or "codex").split(" ", 1)[0]
            if shutil.which(head) is None and not Path(head).exists():
                r.err(f"provider {pname}: không tìm thấy binary {head!r} (Codex CLI) trong PATH")
            else:
                r.info(f"provider {pname}: transport Codex CLI qua {head} — mọi tool qua MCP DDT")
    resolved_family: dict[str, str] = {}
    for role, rc in m.roles.items():
        alias = m.aliases.get(rc.model)
        if alias is None:
            r.err(f"models.roles.{role}.model={rc.model!r}: alias không có hoặc null")
            continue
        prov = m.providers.get(alias.provider)
        if prov is None:
            r.err(f"alias {rc.model}: provider lạ {alias.provider!r}")
            continue
        if not prov.enabled:
            fb = m.aliases.get(rc.fallback or "")
            if fb and m.providers.get(fb.provider, None) and m.providers[fb.provider].enabled:
                r.warn(f"role {role}: provider {alias.provider} tắt → dùng fallback {rc.fallback}")
                alias = fb
            else:
                r.err(f"role {role}: provider {alias.provider} tắt và không có fallback khả dụng")
                continue
        if "tool_use" not in prov.supports and role in ("dev", "planner", "verifier", "security"):
            r.err(f"role {role} cần tool_use nhưng provider {alias.provider} không hỗ trợ")
        resolved_family[role] = alias.family
        r.info(f"{role:14} → {alias.provider}/{alias.model} ({alias.family})")
    for role, rc in m.roles.items():
        if rc.must_differ_family_from and role in resolved_family:
            other = resolved_family.get(rc.must_differ_family_from)
            if other == resolved_family[role]:
                pref = m.aliases.get(rc.prefer or "")
                if pref and pref.family != other:
                    r.info(f"{role}: dùng prefer {rc.prefer} để khác họ {rc.must_differ_family_from}")
                else:
                    r.warn(
                        f"{role} cùng họ model với {rc.must_differ_family_from} ({other}) — "
                        f"điền aliases.{rc.prefer or 'alt_mid'} để khác họ (điểm ăn tiền)"
                    )


def _check_globs(cfg: ResolvedConfig, r: CheckReport) -> None:
    try:
        GlobSet(cfg.sensitive_paths.globs())
        GlobSet(cfg.sensitive_paths.never_touch)
        GlobSet(cfg.policy.exclusive_files)
    except Exception as e:
        r.err(f"glob không hợp lệ: {e}")
    for i, rule in enumerate(cfg.policy.tool_rules):
        if rule.match and rule.match.path_glob:
            try:
                GlobSet([rule.match.path_glob])
            except Exception as e:
                r.err(f"tool_rules[{i}].match.path_glob: {e}")
    if not any(rule.is_default for rule in cfg.policy.tool_rules):
        r.err("tool_rules: thiếu rule default")


def _check_misc(cfg: ResolvedConfig, r: CheckReport) -> None:
    p = cfg.policy
    if p.sandbox.kind == "local":
        r.warn(
            "sandbox.kind=local — KHÔNG phải ranh giới bảo mật thật; ranh giới thật là tool boundary (không git.push, allowlist)"
        )
    if p.wip_limit > p.wip_max:
        r.err(f"wip_limit {p.wip_limit} > wip_max {p.wip_max}")
    if p.autonomy not in p.autonomy_presets:
        r.warn(f"autonomy {p.autonomy!r} không có preset — dùng giá trị trực tiếp")
    for role in p.delegation.roles:
        if role not in cfg.models.roles:
            r.err(f"delegation.roles.{role}: role không có trong models.roles")
    for frm, tos in p.clarify.pairs.items():
        for to in tos:
            if to not in cfg.models.roles:
                r.err(f"clarify.pairs.{frm} → {to}: role lạ")

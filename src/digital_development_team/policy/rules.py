"""Lớp 2 — static rules từ policy.yaml#tool_rules. Rule đầu khớp thắng; default (match=None) bắt buộc."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from digital_development_team.config.models import ToolRule, ToolRuleMatch
from digital_development_team.core.enums import Tier
from digital_development_team.util.globs import GlobSet, expand_braces


@dataclass(frozen=True, slots=True)
class RuleContext:
    tool: str  # canonical, không suffix
    role: str
    args: Mapping[str, Any]
    tier: Tier
    allowed_files: Sequence[str]
    sensitive_globs: Sequence[str]
    spec_values: Mapping[str, str | None]  # spec.test_command, spec.regression_command
    issue_tool_count: int  # số lần tool này đã gọi trong issue

    @property
    def path(self) -> str | None:
        for k in ("path", "file", "target"):
            v = self.args.get(k)
            if isinstance(v, str):
                return v.replace("\\", "/")
        return None

    @property
    def cmd(self) -> str | None:
        for k in ("cmd", "command"):
            v = self.args.get(k)
            if isinstance(v, str):
                return v
        return None

    @property
    def args_text(self) -> str:
        return " ".join(str(v) for v in self.args.values() if isinstance(v, (str, int, float)))


def _tool_matches(pattern: str, tool: str) -> bool:
    for raw in pattern.split("|"):
        alt = raw.strip()
        if alt == tool or (alt.endswith(".*") and tool.startswith(alt[:-1])):
            return True
    return False


@dataclass(frozen=True, slots=True)
class _Compiled:
    rule: ToolRule
    index: int
    args_regex: re.Pattern[str] | None
    path_glob: GlobSet | None


@dataclass(frozen=True, slots=True)
class RuleHit:
    rule: ToolRule
    index: int

    @property
    def rule_id(self) -> str:
        if self.rule.match is None:
            return "default"
        m = self.rule.match
        return f"rule#{self.index}:{m.tool or '*'}"


@dataclass
class RuleTable:
    rules: list[_Compiled] = field(default_factory=list)

    @classmethod
    def from_policy(cls, tool_rules: Sequence[ToolRule]) -> RuleTable:
        compiled: list[_Compiled] = []
        for i, r in enumerate(tool_rules):
            m = r.match
            compiled.append(
                _Compiled(
                    r,
                    i,
                    re.compile(m.args_regex) if m and m.args_regex else None,
                    GlobSet(expand_braces(m.path_glob)) if m and m.path_glob else None,
                )
            )
        if not any(c.rule.match is None for c in compiled):
            raise ValueError("tool_rules thiếu rule default")
        return cls(compiled)

    def first_match(self, ctx: RuleContext) -> RuleHit:
        for c in self.rules:
            if c.rule.match is None or self._matches(c, c.rule.match, ctx):
                return RuleHit(c.rule, c.index)
        raise AssertionError("default bắt buộc")  # pragma: no cover

    @staticmethod
    def _matches(c: _Compiled, m: ToolRuleMatch, ctx: RuleContext) -> bool:
        if m.tool and not _tool_matches(m.tool, ctx.tool):
            return False
        if m.role and m.role != ctx.role:
            return False
        if c.args_regex is not None and not c.args_regex.search(ctx.cmd or ctx.args_text):
            return False
        path = ctx.path
        if c.path_glob is not None and (path is None or not c.path_glob.match(path)):
            return False
        if m.path_not_in:
            if path is None:
                return False
            allowed = ctx.allowed_files if m.path_not_in == "spec.allowed_files" else ()
            if allowed and GlobSet(allowed).match(path):
                return False
        if m.path_in:
            if path is None:
                return False
            globs = ctx.sensitive_globs if m.path_in == "sensitive_paths" else ()
            if not globs or not GlobSet(globs).match(path):
                return False
        if m.tier_below and ctx.tier.rank >= Tier(m.tier_below).rank:
            return False
        if m.cmd_not_in is not None:
            cmd = (ctx.cmd or "").strip()
            allowed_cmds = {v.strip() for k in m.cmd_not_in if (v := ctx.spec_values.get(k))}
            if not cmd or cmd in allowed_cmds:
                return False
        return not (m.count_per_issue_gte is not None and ctx.issue_tool_count < m.count_per_issue_gte)

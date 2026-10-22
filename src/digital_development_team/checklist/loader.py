"""Nạp checklists/**/*.checklist.yaml. applies_when → ddt.expr; biểu thức ngoài dialect (method call, generator)
→ item bị bỏ qua khi select và ghi vào `unsupported` (P3 sửa YAML)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from digital_development_team.config.resources import AssetResolver, build_resolver
from digital_development_team.core.errors import ConfigError, ExpressionError, MissingValue
from digital_development_team.expr.compiler import CompiledExpr, compile_expr
from digital_development_team.util import yamlio

log = logging.getLogger(__name__)

CHECKLISTS_DIR = "checklists"
SEVERITY_ORDER = {"blocking": 0, "major": 1, "minor": 2, "nit": 3}
_METHOD_CONTAINS = re.compile(r"([A-Za-z_][\w.\[\]]*)\.contains\(")


class ChecklistItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    id: str = Field(pattern=r"^[A-Z]+(-[a-z0-9]+)*-[0-9]{3}$")
    text: str
    rationale: str | None = None
    verify: Literal["regex", "test", "diff_inspection", "llm"]
    pattern: str | None = None
    pattern_scope: Literal["added_lines", "removed_lines", "full_diff", "file_content"] = "added_lines"
    test_ref: str | None = None
    threshold: str | None = None
    severity: Literal["blocking", "major", "minor", "nit"] = "major"
    applies_to: list[str] = Field(default_factory=lambda: ["patch"])
    applies_when: str | None = None
    source: str = "base"
    confidence: float = 1.0
    enabled: bool = True
    maps_to: str | None = None

    @property
    def is_machine(self) -> bool:
        return self.verify == "regex"


class ChecklistMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    name: str
    source: str = "base"
    version: int = 1
    lite_ids: list[str] = Field(default_factory=list)


class Checklist(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    meta: ChecklistMeta
    items: list[ChecklistItem]


def normalize_when(expr: str) -> str:
    """x.contains('y') → contains(x, 'y'); .length giữ (parser hỗ trợ)."""
    return _METHOD_CONTAINS.sub(lambda m: f"contains({m.group(1)}, ", expr)


class ChecklistRegistry:
    def __init__(self, resolver: AssetResolver | None = None) -> None:
        self._resolver = resolver or build_resolver()
        self._lists: dict[
            str, Checklist
        ] = {}  # key: "_base/code" · "project-type/web-api" · "domain/banking"
        self._compiled: dict[str, CompiledExpr | None] = {}
        self.unsupported: dict[str, str] = {}  # item id → lý do
        for hit in self._resolver.list(CHECKLISTS_DIR, ".checklist.yaml"):
            data = yamlio.loads(hit.read_text(), source=hit.rel)
            try:
                cl = Checklist.model_validate(data)
            except ValueError as e:
                raise ConfigError(f"{hit.rel}: checklist không hợp lệ: {e}") from e
            key = hit.rel.removeprefix(f"{CHECKLISTS_DIR}/").removesuffix(".checklist.yaml")
            self._lists[key] = cl
            for item in cl.items:
                if item.id in self._compiled:
                    raise ConfigError(f"checklist item trùng id {item.id} ({hit.rel})")
                self._compiled[item.id] = self._compile(item)
                if item.verify == "regex":
                    if not item.pattern:
                        raise ConfigError(f"{item.id}: verify regex nhưng thiếu pattern")
                    try:
                        re.compile(item.pattern, re.M)
                    except re.error as e:
                        raise ConfigError(f"{item.id}: regex lỗi: {e}") from e

    def _compile(self, item: ChecklistItem) -> CompiledExpr | None:
        if not item.applies_when:
            return None
        try:
            return compile_expr(normalize_when(item.applies_when))
        except ExpressionError as e:
            self.unsupported[item.id] = f"applies_when ngoài dialect: {e.message}"
            return None

    # ───── truy vấn ─────
    def keys(self) -> list[str]:
        return sorted(self._lists)

    def get(self, key: str) -> Checklist:
        try:
            return self._lists[key]
        except KeyError:
            raise ConfigError(f"checklist lạ: {key!r}", hint=f"có: {self.keys()}") from None

    def items(self) -> list[ChecklistItem]:
        return [i for cl in self._lists.values() for i in cl.items]

    def lite_security_ids(self) -> list[str]:
        sec = self._lists.get("_base/security")
        return list(sec.meta.lite_ids) if sec else []

    def select(
        self,
        artifact_type: str,
        scope: Mapping[str, Any],
        *,
        project_type: str | None = None,
        domain: str | None = None,
        include_learned: bool = False,
        max_items: int = 30,
        min_confidence: float = 0.4,
    ) -> list[ChecklistItem]:
        """Harness chọn, 0 LLM. Item có applies_when không đánh giá được (thiếu dữ liệu / ngoài dialect) → bỏ."""
        keys = [f"_base/{artifact_type}"]
        if artifact_type == "patch":
            keys += ["_base/code", "_base/test", "_base/security"]
        if project_type:
            keys.append(f"project-type/{project_type}")
        if domain:
            keys.append(f"domain/{domain}")
        seen: set[str] = set()
        out: list[ChecklistItem] = []
        for key in keys:
            cl = self._lists.get(key)
            if cl is None:
                continue
            for item in cl.items:
                if item.id in seen or not item.enabled or item.confidence < min_confidence:
                    continue
                if artifact_type not in item.applies_to:
                    continue
                if item.source == "learned" and not include_learned:
                    continue
                if item.id in self.unsupported:
                    continue
                expr = self._compiled.get(item.id)
                if expr is not None:
                    try:
                        if not expr.as_bool(scope):
                            continue
                    except MissingValue:
                        continue
                seen.add(item.id)
                out.append(item)
        out.sort(key=lambda i: SEVERITY_ORDER[i.severity])
        return out[:max_items]

"""ToolCatalog — _assets/tools/*.tool.yaml: op · risk_tier · allowed_roles · timeout · input_schema · description.

Catalog là DỮ LIỆU (policy tra risk_tier, LLM nhận input_schema). Impl Python ở tools/*.py đăng ký vào ToolRegistry.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from digital_development_team.config.resources import AssetResolver, build_resolver
from digital_development_team.core.enums import RiskTier
from digital_development_team.core.errors import ConfigError
from digital_development_team.core.llm import ToolSpec
from digital_development_team.llm.toolnames import to_wire
from digital_development_team.util import yamlio

TOOLS_DIR = "tools"


class OpCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    risk_tier: RiskTier
    allowed_roles: list[str]
    timeout_sec: int | str = 5  # int | "inherit"
    description: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    sandbox_profile: str | None = None
    gate: str | None = None
    implemented: bool = True


class ToolFileCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    namespace: str
    ops: list[OpCfg]
    sandbox: bool = False
    artifactize: bool = True
    backend: str | None = None
    depth_max: int | None = None


class ToolOp(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str  # canonical fs.read
    namespace: str
    op: str
    risk_tier: RiskTier
    allowed_roles: list[str]
    timeout_s: float | None  # None = inherit
    description: str
    input_schema: dict[str, Any]
    sandbox_profile: str | None
    artifactize: bool
    implemented: bool

    def tool_spec(self) -> ToolSpec:
        schema = dict(self.input_schema)
        schema.setdefault("type", "object")
        schema.setdefault("additionalProperties", False)
        return ToolSpec(
            name=self.name, wire_name=to_wire(self.name), description=self.description, input_schema=schema
        )


class ToolCatalog:
    def __init__(self, resolver: AssetResolver | None = None) -> None:
        self._resolver = resolver or build_resolver()
        self._ops: dict[str, ToolOp] = {}
        for hit in self._resolver.list(TOOLS_DIR, ".tool.yaml"):
            data = yamlio.loads(hit.read_text(), source=hit.rel)
            try:
                cfg = ToolFileCfg.model_validate(data)
            except ValueError as e:
                raise ConfigError(f"{hit.rel}: tool yaml không hợp lệ: {e}") from e
            for op in cfg.ops:
                name = f"{cfg.namespace}.{op.name}"
                if name in self._ops:
                    raise ConfigError(f"tool op trùng: {name}")
                self._ops[name] = ToolOp(
                    name=name,
                    namespace=cfg.namespace,
                    op=op.name,
                    risk_tier=op.risk_tier,
                    allowed_roles=op.allowed_roles,
                    timeout_s=None if op.timeout_sec == "inherit" else float(op.timeout_sec),
                    description=op.description,
                    input_schema=op.input_schema,
                    sandbox_profile=op.sandbox_profile,
                    artifactize=cfg.artifactize,
                    implemented=op.implemented,
                )
        if not self._ops:
            raise ConfigError("không có tool nào trong tools/")

    def add_ops(self, ops: Iterable[ToolOp]) -> None:
        """Op dựng lúc chạy (MCP server ngoài từ snapshot đã ghim)."""
        for op in ops:
            if op.name in self._ops:
                raise ConfigError(f"tool op trùng: {op.name}")
            self._ops[op.name] = op

    def names(self) -> list[str]:
        return sorted(self._ops)

    def op(self, name: str) -> ToolOp | None:
        return self._ops.get(name.split(":", 1)[0])

    def get(self, name: str) -> ToolOp:
        op = self.op(name)
        if op is None:
            raise ConfigError(f"tool lạ: {name!r}", hint=f"có: {self.names()}")
        return op

    def specs_for(self, tool_names: list[str]) -> tuple[ToolSpec, ...]:
        """ToolSpec cố định cả phiên cho một agent (theo frontmatter tools[], bỏ suffix :profile)."""
        seen: set[str] = set()
        out: list[ToolSpec] = []
        for raw in tool_names:
            name = raw.split(":", 1)[0]
            if name in seen:
                continue
            seen.add(name)
            out.append(self.get(name).tool_spec())
        return tuple(out)

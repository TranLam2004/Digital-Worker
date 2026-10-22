"""AgentSpec — frontmatter `.agent.md` → Pydantic. Markdown phía sau = JD (system prompt).

Field chưa biết được GIỮ (extra="allow") để file agent có thể thêm metadata (metrics, owner, modes…) mà không sửa code;
field harness dùng thì được validate chặt. `models.yaml#roles` thắng `model` trong frontmatter (config check cảnh báo lệch).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from digital_development_team.blackboard.slots import SLOTS
from digital_development_team.config.resources import AssetResolver, build_resolver
from digital_development_team.core.enums import OutputMode, PermissionMode, Stage
from digital_development_team.core.errors import ConfigError
from digital_development_team.llm.toolnames import CANONICAL_RE
from digital_development_team.util import yamlio

AGENTS_DIR = "agents"
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.S)
_READ_RE = re.compile(r"^(?P<slot>[a-z][a-z0-9_]*(?:\.(?:yaml|json|jsonl)|/))(?::(?P<field>[a-z_]+))?$")


@dataclass(frozen=True, slots=True)
class ToolGrant:
    """'exec.run:readonly' → name='exec.run', profile='readonly'."""

    name: str
    profile: str | None = None

    @staticmethod
    def parse(raw: str) -> ToolGrant:
        name, _, profile = raw.partition(":")
        if not CANONICAL_RE.match(name):
            raise ValueError(f"tool không đúng dạng namespace.op: {raw!r}")
        return ToolGrant(name, profile or None)

    @property
    def namespace(self) -> str:
        return self.name.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class ReadGrant:
    """'evidence.json:failure_feedback_for_dev' → slot + field; 'patch/:stat' → projection 'stat'."""

    slot: str
    field: str | None = None

    @staticmethod
    def parse(raw: str) -> ReadGrant:
        m = _READ_RE.match(raw)
        if not m:
            raise ValueError(f"reads không hợp lệ: {raw!r}")
        return ReadGrant(m["slot"], m["field"])


class SkillRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    skill: str
    when: str


class SkillsCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    always: list[str] = Field(default_factory=list)
    conditional: list[SkillRef] = Field(default_factory=list)


class CondenserCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    strategy: str = "default"
    evict_at: float = 0.80
    compress_at: float = 0.90
    keep_last_turns: int = 6
    memory_block_tokens: int = 1200
    artifactize_threshold: int = 2000


class DelegationCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    enabled: bool = False
    max_subagents: int = 0
    sub_budget_pct: float = 0.4
    sub_tools: list[str] = Field(default_factory=list)
    review_checklists: list[str] = Field(default_factory=list)


class ClarifyCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    can_ask: list[str] = Field(default_factory=list)
    can_answer: bool = False


class ChecklistsCfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_on: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)


class StageMode(BaseModel):
    """Một agent làm thêm stage khác với hồ sơ hẹp hơn (vd Dev ở ANSWER: chỉ đọc, ghi answer.yaml).

    Trường nào để trống thì giữ như hồ sơ chính. tools phải là tập con của tools chính: allowlist không nở ra theo stage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    task: str | None = None
    output_mode: OutputMode | None = None
    tools: list[str] | None = None
    reads: list[str] | None = None
    writes: list[str] | None = None
    context_extras: list[str] | None = None
    skills: SkillsCfg | None = None


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    role: str
    version: int = 1
    stage: str | list[str]  # Stage · [Stage, Stage] · "async"
    model: str
    fallback_model: str | None = None
    rework_model_escalation: bool = (
        False  # rework ≥ policy.rework_model_escalation_at → models.yaml#<role>_rework
    )
    model_family_must_differ_from: str | None = None
    permission_mode: PermissionMode
    output_mode: OutputMode
    task: str
    context_extras: list[str] = Field(default_factory=list)
    tools: list[str]
    skills: SkillsCfg = SkillsCfg()
    reads: list[str] = Field(default_factory=list)
    writes: list[str]
    condenser: CondenserCfg = CondenserCfg()
    budget_ref: str
    merge_authority: str = "none"
    escalation_allowed: list[str] = Field(default_factory=list)
    delegation: DelegationCfg = DelegationCfg()
    clarify: ClarifyCfg = ClarifyCfg()
    checklists: ChecklistsCfg = ChecklistsCfg()
    overlays: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    owner: str | None = None
    stage_modes: dict[str, StageMode] = Field(default_factory=dict)

    # ───── validate ─────
    @field_validator("tools")
    @classmethod
    def _tools_ok(cls, v: list[str]) -> list[str]:
        for t in v:
            ToolGrant.parse(t)
        if len(set(v)) != len(v):
            raise ValueError("tools trùng")
        return v

    @field_validator("reads")
    @classmethod
    def _reads_ok(cls, v: list[str]) -> list[str]:
        for r in v:
            ReadGrant.parse(r)
        return v

    @model_validator(mode="after")
    def _stage_and_writes(self) -> AgentSpec:
        for s in self.stages:
            if s != "async":
                Stage(s)
        if not self.writes:
            raise ValueError("writes rỗng — agent phải có writes[0]")
        if self.permission_mode is not PermissionMode.MEMORY_ONLY:
            unknown = [w for w in self.writes if w not in SLOTS]
            if unknown:
                raise ValueError(f"writes ngoài slot registry: {unknown}")
        return self

    @model_validator(mode="after")
    def _modes_ok(self) -> AgentSpec:
        base = {ToolGrant.parse(t).name for t in self.tools}
        for stage, mode in self.stage_modes.items():
            if stage not in self.stages:
                raise ValueError(f"stage_modes.{stage}: stage không có trong stage của agent {self.stages}")
            extra = [t for t in mode.tools or [] if ToolGrant.parse(t).name not in base]
            if extra:
                raise ValueError(f"stage_modes.{stage}: tool ngoài allowlist chính {extra}")
            for r in mode.reads or []:
                ReadGrant.parse(r)
            if mode.writes is not None:
                if not mode.writes:
                    raise ValueError(f"stage_modes.{stage}: writes rỗng")
                unknown = [w for w in mode.writes if w not in SLOTS]
                if unknown:
                    raise ValueError(f"stage_modes.{stage}: writes ngoài slot registry {unknown}")
        return self

    def for_stage(self, stage: Stage | str) -> AgentSpec:
        """Hồ sơ dùng cho stage này: hồ sơ chính, hoặc bản đã áp `stage_modes[stage]`."""
        key = stage.value if isinstance(stage, Stage) else stage
        mode = self.stage_modes.get(key)
        if mode is None:
            return self
        update = {k: v for k, v in mode.model_dump(exclude_none=True).items()}
        if mode.skills is not None:
            update["skills"] = mode.skills
        return self.model_copy(update=update)

    # ───── tiện ích ─────
    @property
    def stages(self) -> list[str]:
        return [self.stage] if isinstance(self.stage, str) else list(self.stage)

    @property
    def primary_stage(self) -> Stage | None:
        s = self.stages[0]
        return None if s == "async" else Stage(s)

    @property
    def output_slot(self) -> str:
        return self.writes[0]

    @property
    def tool_grants(self) -> list[ToolGrant]:
        return [ToolGrant.parse(t) for t in self.tools]

    @property
    def tool_names(self) -> list[str]:
        return [g.name for g in self.tool_grants]

    def tool_profile(self, name: str) -> str | None:
        return next((g.profile for g in self.tool_grants if g.name == name), None)

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names

    @property
    def read_grants(self) -> list[ReadGrant]:
        return [ReadGrant.parse(r) for r in self.reads]

    @property
    def read_slots(self) -> list[str]:
        return [g.slot for g in self.read_grants]

    @property
    def is_pipeline_agent(self) -> bool:
        return self.primary_stage is not None

    @property
    def extra_fields(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


def parse_agent_file(text: str, *, source: str = "<agent>") -> tuple[AgentSpec, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ConfigError(f"{source}: thiếu frontmatter ---…---")
    data = yamlio.loads(m.group(1), source=source)
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: frontmatter phải là mapping")
    try:
        spec = AgentSpec.model_validate(data)
    except ValueError as e:
        raise ConfigError(f"{source}: frontmatter không hợp lệ: {e}") from e
    return spec, m.group(2).strip() + "\n"


class AgentRegistry:
    """Nạp mọi agents/*.agent.md qua AssetResolver (packaged → overrides → --config-dir)."""

    def __init__(self, resolver: AssetResolver | None = None) -> None:
        self._resolver = resolver or build_resolver()
        self._specs: dict[str, AgentSpec] = {}
        self._jd: dict[str, str] = {}
        self._sources: dict[str, str] = {}
        for hit in self._resolver.list(AGENTS_DIR, ".agent.md"):
            spec, jd = parse_agent_file(hit.read_text(), source=hit.rel)
            if spec.name in self._specs:
                raise ConfigError(f"agent trùng tên {spec.name!r}: {hit.rel} và {self._sources[spec.name]}")
            self._specs[spec.name] = spec
            self._jd[spec.name] = jd
            self._sources[spec.name] = hit.rel
        if not self._specs:
            raise ConfigError("không có agent nào trong agents/")

    def names(self) -> list[str]:
        return sorted(self._specs)

    def grant(self, extra: Mapping[str, Sequence[str]]) -> None:
        """Thêm tool cấp lúc chạy (MCP server khai `roles`) vào hồ sơ chính của role."""
        for role, tools in extra.items():
            spec = self._specs.get(role)
            if spec is None:
                continue
            new = [t for t in tools if t not in spec.tools]
            if new:
                self._specs[role] = spec.model_copy(update={"tools": [*spec.tools, *new]})

    def revoke(self, patterns: Sequence[str]) -> None:
        """Gỡ tool khớp pattern (fnmatch, vd `agent.*`) khỏi mọi role, cả stage_modes — policy.tool_denylist.
        Mỗi schema tool gửi kèm MỌI lượt gọi model: tool không dùng tới là token mất trắng (thi đấu chấm credit)."""
        if not patterns:
            return

        def keep(grants: Sequence[str]) -> list[str]:
            return [g for g in grants if not any(fnmatchcase(ToolGrant.parse(g).name, p) for p in patterns)]

        for name, spec in list(self._specs.items()):
            modes = {
                k: m.model_copy(update={"tools": keep(m.tools)}) if m.tools is not None else m
                for k, m in spec.stage_modes.items()
            }
            self._specs[name] = spec.model_copy(update={"tools": keep(spec.tools), "stage_modes": modes})

    def __iter__(self) -> Iterator[AgentSpec]:
        return iter(self._specs[n] for n in self.names())

    def get(self, name: str) -> AgentSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ConfigError(f"agent lạ: {name!r}", hint=f"có: {self.names()}") from None

    def jd(self, name: str) -> str:
        self.get(name)
        return self._jd[name]

    def source(self, name: str) -> str:
        return self._sources[name]

    def for_stage(self, stage: Stage) -> AgentSpec:
        matches = [s for s in self if s.primary_stage is stage]
        if len(matches) != 1:
            raise ConfigError(
                f"stage {stage.value}: {len(matches)} agent (cần đúng 1)", agents=[m.name for m in matches]
            )
        return matches[0]

    def pipeline_agents(self) -> list[AgentSpec]:
        return [s for s in self if s.is_pipeline_agent]

"""Skills — frontmatter + 4 mục (`## When to apply` · `## Procedure` · `## Known pitfalls` · `## Output checklist`).
Nạp lazy vào CONTEXT khi trigger khớp (30-skills/_skill-system). Nội dung skill viết bằng tiếng Anh (ngôn ngữ của agent).

Hai layout file đều được nhận:
  skills/core/<name>.SKILL.md            → skill DDT viết tay (source: core)
  skills/imported/<name>/SKILL.md        → chuẩn Agent Skills (agentskills.io), rút gọn về format DDT (source: imported)
Thư mục `scripts/`, `references/`, file LICENSE/README cạnh SKILL.md bị bỏ qua — skill DDT chỉ là text.
Ba nguồn: core (≤1300 token) · imported (≤1000, bắt buộc origin + license) · learned (≤400, P3).

Trigger:
  always                              → luôn (nếu role trong used_by)
  "<expr>"                            → ddt.expr trên scope brief (intake, spec, repo.has_repo_md…); thiếu dữ liệu → KHÔNG nạp
  "!exists('memory/REPO.md')"         → dịch sang `not default(repo.has_repo_md, false)`
  needs_user_ask | manual             → nạp khi agent có tool user.ask (rẻ hơn cơ chế "agent tự lấy")
  "memory.skills_for(...)"            → learned skill — TODO(P3-#30) memory; hiện không nạp
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from digital_development_team.config.resources import AssetResolver, build_resolver
from digital_development_team.core.errors import ConfigError, ExpressionError, MissingValue
from digital_development_team.expr.compiler import CompiledExpr, compile_expr
from digital_development_team.util import yamlio
from digital_development_team.util.text import estimate_tokens

SKILLS_DIR = "skills"
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.S)
_EXISTS_RE = re.compile(r"!?\s*exists\(\s*'([^']+)'\s*\)")
MAX_TOKENS = {"core": 1300, "imported": 1000, "learned": 400}
MAX_BRIEF_SKILL_TOKENS = 4500  # tổng skill một brief; always nạp trước nên không bao giờ bị đẩy ra
MANUAL_TRIGGERS = {"manual", "needs_user_ask"}
SKILL_FILE = "SKILL.md"
IMPORTED = "imported"
LOCK_REL = "skills/skills.lock.json"  # agents/skill_import.py ghi khi import skill ngoài
PACKAGED_LAYER = "packaged"
log = logging.getLogger(__name__)


def is_skill_file(rel: str) -> bool:
    """`x.SKILL.md` hoặc `<dir>/SKILL.md`. README.md, LICENSE, scripts/, references/ → False."""
    base = rel.replace("\\", "/").rsplit("/", 1)[-1]
    return base.endswith(".SKILL.md") or base == SKILL_FILE


class SkillMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str
    version: int = 1
    used_by: list[str] = Field(default_factory=list)
    trigger: str = "always"
    budget_tokens: int = 600
    source: str = "core"
    confidence: float | None = None  # learned
    # provenance — bắt buộc với source: imported
    origin: str | None = None  # URL upstream, pin commit
    license: str | None = None  # MIT | CC-BY-SA-4.0 | …
    imported_at: str | None = None  # ISO date (YAML có thể parse thành date → ép về str)
    adapted: bool = False  # đã rút gọn/chuyển sang tool DDT

    @field_validator("imported_at", mode="before")
    @classmethod
    def _date_to_str(cls, v: object) -> object:
        return v.isoformat() if hasattr(v, "isoformat") else v

    @model_validator(mode="after")
    def _imported_provenance(self) -> SkillMeta:
        if self.source == IMPORTED and (not self.origin or not self.license):
            raise ValueError("skill imported bắt buộc có `origin` (URL + commit) và `license`")
        return self


@dataclass(frozen=True, slots=True)
class SkillSpec:
    meta: SkillMeta
    body: str
    rel: str
    layer: str

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.body)

    @property
    def is_manual(self) -> bool:
        return self.meta.trigger.strip() in MANUAL_TRIGGERS

    @property
    def is_always(self) -> bool:
        return self.meta.trigger.strip() == "always"

    def one_liner(self) -> str:
        return f"- {self.name}: {self.meta.description}"


def normalize_trigger(trigger: str) -> str | None:
    """Trigger → biểu thức ddt.expr, hoặc None nếu không phải biểu thức (always/manual/learned)."""
    t = trigger.strip()
    if t == "always" or t in MANUAL_TRIGGERS or "memory.skills_for" in t:
        return None
    # !exists('memory/REPO.md') → not default(repo.has_repo_md, false)
    t = _EXISTS_RE.sub(
        lambda m: ("not " if m.group(0).startswith("!") else "") + "default(repo.has_repo_md, false)", t
    )
    return t


class SkillRegistry:
    def __init__(self, resolver: AssetResolver | None = None) -> None:
        self._resolver = resolver or build_resolver()
        self._skills: dict[str, SkillSpec] = {}
        self._compiled: dict[str, CompiledExpr | None] = {}
        self.warnings: list[str] = []
        lock = self._lock()
        for hit in self._resolver.list(SKILLS_DIR, ".md"):
            if not is_skill_file(hit.rel):
                continue
            try:
                self._load(hit, lock)
            except ConfigError as e:
                if hit.layer == PACKAGED_LAYER:
                    raise
                # skill ở lớp override (người sửa, import từ ngoài) hỏng → bỏ đúng skill đó, không sập cả registry
                self.warnings.append(f"{hit.rel}: bỏ qua — {e.message}")
                log.warning("skill %s bị bỏ qua: %s", hit.rel, e.message)

    def _lock(self) -> dict[str, Any]:
        if not self._resolver.exists(LOCK_REL):
            return {}
        try:
            data = json.loads(self._resolver.read_text(LOCK_REL))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _load(self, hit: Any, lock: Mapping[str, Any]) -> None:
        text = hit.read_text()
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ConfigError(f"{hit.rel}: thiếu frontmatter")
        data = yamlio.loads(m.group(1), source=hit.rel)
        try:
            meta = SkillMeta.model_validate(data)
        except ValueError as e:
            raise ConfigError(f"{hit.rel}: frontmatter skill không hợp lệ: {e}") from e
        pinned = lock.get(meta.name) if meta.source == IMPORTED else None
        if (
            isinstance(pinned, Mapping)
            and pinned.get("sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()
        ):
            raise ConfigError(f"{hit.rel}: nội dung khác skills.lock.json (bị sửa sau khi import)")
        spec = SkillSpec(meta, m.group(2).strip() + "\n", hit.rel, hit.layer)
        cap = MAX_TOKENS.get(meta.source, 1000)
        if spec.tokens > cap * 1.5:  # ước lượng chars/4 — nới 50%
            raise ConfigError(
                f"{hit.rel}: skill ~{spec.tokens} token > {cap}", hint="cắt bớt hoặc tách skill"
            )
        if meta.name in self._skills:
            raise ConfigError(f"skill trùng tên {meta.name!r}")
        expr = normalize_trigger(meta.trigger)
        try:
            compiled = compile_expr(expr) if expr else None
        except ExpressionError as e:
            raise ConfigError(f"{hit.rel}: trigger không hợp lệ: {e}") from e
        self._skills[meta.name] = spec
        self._compiled[meta.name] = compiled

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> SkillSpec:
        try:
            return self._skills[name]
        except KeyError:
            raise ConfigError(f"skill lạ: {name!r}", hint=f"có: {self.names()}") from None

    def has(self, name: str) -> bool:
        return name in self._skills

    def for_role(self, role: str) -> list[SkillSpec]:
        """Skill role được phép thấy (tên + 1 dòng trong system prompt)."""
        return [s for s in (self._skills[n] for n in self.names()) if role in s.meta.used_by]

    def select(
        self,
        role: str,
        *,
        always: list[str],
        conditional: list[tuple[str, str]],
        scope: Mapping[str, Any],
        has_user_ask: bool,
        max_total_tokens: int = MAX_BRIEF_SKILL_TOKENS,
    ) -> tuple[list[SkillSpec], list[str]]:
        """Skill nạp vào brief + ghi chú vì sao bỏ. Thứ tự: always → conditional khớp → manual (nếu user.ask)."""
        chosen: list[SkillSpec] = []
        notes: list[str] = []

        def add(s: SkillSpec, why: str) -> None:
            if s in chosen:
                return
            if sum(x.tokens for x in chosen) + s.tokens > max_total_tokens:
                notes.append(f"{s.name}: bỏ, vượt {max_total_tokens} token")
                return
            chosen.append(s)
            notes.append(f"{s.name}: {why}")

        for name in always:
            if name.startswith("learned/"):
                notes.append(f"{name}: learned — TODO(P3)")
                continue
            add(self.get(name), "always")
        for name, when in conditional:
            if name.startswith("learned/") or "memory.skills_for" in when:
                notes.append(f"{name}: learned — TODO(P3)")
                continue
            expr = normalize_trigger(when)
            if expr is None:
                if has_user_ask:
                    add(self.get(name), "manual + có user.ask")
                continue
            try:
                ok = compile_expr(expr).as_bool(scope)
            except MissingValue as e:
                notes.append(f"{name}: thiếu dữ liệu ({e.message}) → không nạp")
                continue
            if ok:
                add(self.get(name), f"khớp: {when}")
            else:
                notes.append(f"{name}: không khớp {when}")
        # skill manual role được dùng (escalation-form) khi có user.ask
        if has_user_ask:
            for s in self.for_role(role):
                if s.is_manual:
                    add(s, "manual + có user.ask")
        return chosen, notes


_WORD = re.compile(r"[a-z0-9]+")
_HEADING = re.compile(r"^#{1,4}\s+(.+)$", re.M)


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class SkillIndex:
    """skill.search / skill.load (tools/skill.py SkillLookup): BM25 nhẹ trên tên + mô tả + tiêu đề mục, không vector DB.
    Agent tự nạp skill khi trigger cố định không chọn đúng skill cho task lạ."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._reg = registry
        self._docs: dict[str, list[str]] = {}
        for name in registry.names():
            s = registry.get(name)
            heads = " ".join(_HEADING.findall(s.body))
            self._docs[name] = _words(f"{name.replace('-', ' ')} {s.meta.description} {heads}")
        n = len(self._docs) or 1
        self._avg = sum(len(d) for d in self._docs.values()) / n
        self._df: dict[str, int] = {}
        for d in self._docs.values():
            for w in set(d):
                self._df[w] = self._df.get(w, 0) + 1
        self._n = n

    def search(self, query: str, *, role: str, k: int) -> list[dict[str, Any]]:
        import math

        q = set(_words(query))
        scored: list[tuple[float, str]] = []
        for name, doc in self._docs.items():
            score = 0.0
            for w in q:
                tf = doc.count(w)
                if not tf:
                    continue
                idf = math.log(1 + (self._n - self._df[w] + 0.5) / (self._df[w] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(doc) / (self._avg or 1)))
            if score > 0:
                scored.append((score + (0.5 if role in self._reg.get(name).meta.used_by else 0.0), name))
        out = []
        for score, name in sorted(scored, reverse=True)[:k]:
            s = self._reg.get(name)
            out.append(
                {
                    "name": name,
                    "description": s.meta.description,
                    "for_roles": list(s.meta.used_by),
                    "source": s.meta.source,
                    "tokens": s.tokens,
                    "score": round(score, 2),
                }
            )
        return out

    def load(self, name: str, *, role: str) -> dict[str, Any] | None:
        if not self._reg.has(name):
            return None
        s = self._reg.get(name)
        return {
            "name": name,
            "description": s.meta.description,
            "for_roles": list(s.meta.used_by),
            "source": s.meta.source,
            "tokens": s.tokens,
            "body": s.body,
        }

"""Quét nội dung Learner ghi vào memory trước khi agent khác thấy (chống memory poisoning). 0 LLM.

Skill học được phải: frontmatter hợp lệ, tên `learned-<slug>`, gắn module hoặc loại issue, đủ 4 mục như skill core,
≤ skill_max_tokens, không có câu lệnh kiểu prompt injection, không có secret, chỉ nhắc tool có thật, lệnh trong khối code
qua được command guard, URL nằm trong allowlist mạng. Fact/lesson: một câu, không injection, không secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from digital_development_team.config.models import CommandGuardCfg, NetworkCfg
from digital_development_team.memory import NO_MEMORY_ROLES
from digital_development_team.policy.command import check_command
from digital_development_team.policy.hooks import injection_scan, redact_secrets
from digital_development_team.policy.netguard import check_url
from digital_development_team.util import yamlio
from digital_development_team.util.text import estimate_tokens

NAME_RE = re.compile(r"^learned-[a-z0-9][a-z0-9-]{2,60}$")
SECTIONS = ("## When to apply", "## Procedure", "## Known pitfalls", "## Output checklist")
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.S)
_TOOL_NS = "fs|exec|test|git|web|docs|pkg|skill|memory|blackboard|security|codegraph|llm|user|agent|mcp"
_TOOL_REF = re.compile(rf"\b(?:{_TOOL_NS})\.[a-z_]+(?:__[a-z0-9_]+)?\b")
_FENCE = re.compile(r"```([a-zA-Z]*)\n(.*?)```", re.S)
_SHELL_LANGS = {"", "bash", "sh", "shell", "console", "powershell", "ps1", "cmd", "bat"}
_URL = re.compile(r"\bhttps?://[^\s)>\]\"'`]+")
MAX_FACT_CHARS = 200
MAX_LESSON_CHARS = 300


@dataclass(frozen=True)
class ScanContext:
    tool_names: frozenset[str] = frozenset()
    command_guard: CommandGuardCfg = field(default_factory=CommandGuardCfg)
    network: NetworkCfg = field(default_factory=NetworkCfg)
    max_tokens: int = 400


def split_skill(text: str) -> tuple[dict[str, Any], str] | None:
    m = _FRONTMATTER.match(text.replace("\r\n", "\n").strip() + "\n")
    if m is None:
        return None
    try:
        meta = yamlio.loads(m.group(1)) or {}
    except Exception:
        return None
    return (dict(meta), m.group(2)) if isinstance(meta, dict) else None


def scan_text(text: str, *, max_chars: int) -> list[str]:
    """Fact / lesson: một đoạn ngắn, không lệnh cho agent, không secret."""
    problems: list[str] = []
    if len(text) > max_chars:
        problems.append(f"longer than {max_chars} characters")
    problems += [f"injection-like text: {s!r}" for s in injection_scan(text)]
    if redact_secrets(text)[1]:
        problems.append("contains something that looks like a secret")
    return problems


def scan_skill(text: str, ctx: ScanContext) -> tuple[dict[str, Any], str, list[str]]:
    """(meta, body, problems). problems rỗng ⇔ được lên candidate."""
    parts = split_skill(text)
    if parts is None:
        return {}, text, ["missing or invalid YAML frontmatter (--- … ---)"]
    meta, body = parts
    problems: list[str] = []
    name = str(meta.get("name") or "")
    if not NAME_RE.match(name):
        problems.append("name must look like learned-<slug> (lowercase, digits, hyphens)")
    if not str(meta.get("description") or "").strip():
        problems.append("description is required (one sentence)")
    used_by = meta.get("used_by") or ["dev"]
    if not isinstance(used_by, list) or not used_by or any(r in NO_MEMORY_ROLES for r in used_by):
        problems.append("used_by must list roles other than verifier/security")
    if str(meta.get("module") or "*") == "*" and str(meta.get("issue_type") or "*") == "*":
        problems.append("bind the skill to a module or an issue_type (not both '*')")
    for sec in SECTIONS:
        if sec not in body:
            problems.append(f"missing section {sec!r}")
    tokens = estimate_tokens(body)
    if tokens > ctx.max_tokens:
        problems.append(f"body is {tokens} tokens, limit {ctx.max_tokens}")
    problems += [f"injection-like text: {s!r}" for s in injection_scan(text)]
    if redact_secrets(text)[1]:
        problems.append("contains something that looks like a secret")
    if ctx.tool_names:
        unknown = sorted({t for t in _TOOL_REF.findall(body) if t not in ctx.tool_names})
        if unknown:
            problems.append(f"mentions tools that do not exist: {', '.join(unknown[:5])}")
    for lang, block in _FENCE.findall(body):
        if lang.lower() not in _SHELL_LANGS:
            continue
        for line in block.splitlines():
            cmd = line.strip().removeprefix("$ ").strip()
            if not cmd or cmd.startswith("#"):
                continue
            v = check_command(cmd, ctx.command_guard)
            if not v.ok:
                problems.append(f"command blocked by policy ({v.rule}): {cmd[:80]}")
    for url in _URL.findall(text):
        nv = check_url(url.rstrip(".,;"), ctx.network)
        if not nv.ok:
            problems.append(f"URL not allowed ({nv.rule}): {url[:80]}")
    return meta, body, problems


__all__ = [
    "MAX_FACT_CHARS",
    "MAX_LESSON_CHARS",
    "NAME_RE",
    "SECTIONS",
    "ScanContext",
    "scan_skill",
    "scan_text",
    "split_skill",
]

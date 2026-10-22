"""Import skill từ repo ngoài (Phase 10): `ddt skill import github:<owner>/<repo>/<path>@<commit>`.

- Ghim commit đủ 40 hex. Chỉ lấy `<path>/SKILL.md` và `LICENSE` ở gốc repo, qua raw.githubusercontent.com (phải nằm
  trong policy.network.allow) — không chạy script, không lấy file khác.
- License phải thuộc loại cho phép (nhận diện từ nội dung LICENSE); không rõ → từ chối.
- Quét: frontmatter, đủ nội dung, ≤ 1000 token (cap skill imported), không injection/secret, URL trong allowlist,
  tool được nhắc phải có thật (chỉ cảnh báo — skill ngoài viết cho hệ khác).
- Ghi `<home>/overrides/skills/imported/<name>/SKILL.md` với frontmatter DDT (source imported, origin, license,
  imported_at, trigger manual → chỉ tới được qua skill.search/load) + `<home>/overrides/skills/skills.lock.json`.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digital_development_team.config.models import NetworkCfg
from digital_development_team.config.resources import HOME_OVERRIDES_DIR
from digital_development_team.core.errors import ConfigError
from digital_development_team.memory.skill_scan import split_skill
from digital_development_team.policy.hooks import injection_scan, redact_secrets
from digital_development_team.policy.netguard import check_url
from digital_development_team.util import yamlio
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.text import estimate_tokens

SPEC_RE = re.compile(
    r"^github:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?P<path>[\w./-]+?)/?@(?P<sha>[0-9a-f]{40})$"
)
LOCK_REL = "skills/skills.lock.json"
MAX_TOKENS = 1000
_NAME = re.compile(r"[^a-z0-9-]+")
_URL = re.compile(r"\bhttps?://[^\s)>\]\"'`]+")
_TOOL_REF = re.compile(
    r"\b(?:fs|exec|test|git|web|docs|pkg|skill|memory|blackboard|security|codegraph|llm|user|agent)\.[a-z_]+\b"
)
# (SPDX, dấu hiệu trong LICENSE) — thứ tự: cụ thể trước
LICENSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Apache-2.0", ("apache license", "version 2.0")),
    ("MIT", ("permission is hereby granted, free of charge",)),
    ("BSD-3-Clause", ("redistribution and use in source and binary forms", "neither the name")),
    ("BSD-2-Clause", ("redistribution and use in source and binary forms",)),
    ("ISC", ("permission to use, copy, modify, and/or distribute this software",)),
    ("CC-BY-4.0", ("creative commons attribution 4.0",)),
    ("CC0-1.0", ("cc0 1.0 universal",)),
    ("Unlicense", ("this is free and unencumbered software released into the public domain",)),
)

Fetch = Callable[[str], str]


@dataclass
class ImportResult:
    name: str
    path: Path
    origin: str
    license: str
    tokens: int
    warnings: list[str] = field(default_factory=list)


def detect_license(text: str) -> str | None:
    low = " ".join(text.lower().split())
    for spdx, marks in LICENSES:
        if all(m in low for m in marks):
            return spdx
    return None


def default_fetch(network: NetworkCfg) -> Fetch:
    from digital_development_team.tools.web import tls_context

    ctx = tls_context()

    def fetch(url: str) -> str:
        v = check_url(url, network)
        if not v.ok:
            raise ConfigError(
                f"không tải được {url}: {v.reason}", hint="host phải nằm trong policy.network.allow"
            )
        with urllib.request.urlopen(url, timeout=30, context=ctx) as r:  # noqa: S310 - url đã qua netguard
            return str(r.read(400_000).decode(ENCODING, errors="replace"))

    return fetch


def import_skill(
    spec: str,
    *,
    home: Path,
    fetch: Fetch,
    network: NetworkCfg,
    tool_names: Sequence[str],
    roles: Sequence[str] = ("dev",),
    now: str,
    force: bool = False,
) -> ImportResult:
    m = SPEC_RE.match(spec.strip())
    if m is None:
        raise ConfigError(
            "spec phải dạng github:<owner>/<repo>/<path>@<commit 40 hex>",
            hint="ghim commit đầy đủ để nội dung không đổi sau khi import",
        )
    owner, repo, path, sha = m.group("owner"), m.group("repo"), m.group("path").strip("/"), m.group("sha")
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}"
    body_url = f"{base}/{path}/SKILL.md" if not path.endswith(".md") else f"{base}/{path}"
    text = fetch(body_url)
    lic_text = fetch(f"{base}/LICENSE")
    spdx = detect_license(lic_text)
    if spdx is None:
        raise ConfigError(
            "không nhận ra license cho phép", hint=f"cho phép: {', '.join(x for x, _ in LICENSES)}"
        )
    parts = split_skill(text)
    meta: dict[str, Any] = parts[0] if parts else {}
    body = (parts[1] if parts else text).strip()
    raw_name = str(meta.get("name") or path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md"))
    name = _NAME.sub("-", raw_name.lower()).strip("-")[:60] or "imported-skill"
    tokens = estimate_tokens(body)
    problems: list[str] = []
    if len(body) < 200:
        problems.append("nội dung quá ngắn")
    if tokens > MAX_TOKENS:
        problems.append(f"{tokens} token > {MAX_TOKENS} (cap skill imported) — rút gọn rồi import bản riêng")
    problems += [f"câu kiểu injection: {s!r}" for s in injection_scan(text)]
    if redact_secrets(text)[1]:
        problems.append("có thứ trông như secret")
    warnings = [
        f"URL ngoài allowlist: {u[:80]}"
        for u in _URL.findall(body)
        if not check_url(u.rstrip(".,;"), network).ok
    ]
    unknown = sorted({t for t in _TOOL_REF.findall(body) if t not in set(tool_names)})
    if unknown:
        warnings.append(f"nhắc tool không có ở DDT: {', '.join(unknown[:5])} — agent sẽ không gọi được")
    if problems:
        raise ConfigError("skill không qua quét: " + "; ".join(problems))
    dest = home / HOME_OVERRIDES_DIR / "skills" / "imported" / name / "SKILL.md"
    if dest.exists() and not force:
        raise ConfigError(f"đã có skill imported {name}", hint="--force để thay")
    origin = f"https://github.com/{owner}/{repo}/tree/{sha}/{path}"
    front = {
        "name": name,
        "description": " ".join(str(meta.get("description") or f"Imported skill {name}").split())[:300],
        "version": 1,
        "used_by": list(roles),
        "trigger": "manual",
        "budget_tokens": min(MAX_TOKENS, max(200, tokens)),
        "source": "imported",
        "origin": origin,
        "license": spdx,
        "imported_at": now[:10],
        "adapted": False,
    }
    content = f"---\n{yamlio.dumps(front).strip()}\n---\n\n{body}\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(dest, content, fsync=False)
    write_text_atomic(dest.parent / "LICENSE", lic_text, fsync=False)
    lock_path = home / HOME_OVERRIDES_DIR / LOCK_REL
    lock = read_lock(lock_path)
    lock[name] = {
        "origin": origin,
        "commit": sha,
        "license": spdx,
        "sha256": hashlib.sha256(content.encode(ENCODING)).hexdigest(),
        "imported_at": now,
    }
    write_text_atomic(lock_path, json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True), fsync=False)
    return ImportResult(name, dest, origin, spdx, tokens, warnings)


def read_lock(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "LICENSES",
    "LOCK_REL",
    "ImportResult",
    "default_fetch",
    "detect_license",
    "import_skill",
    "read_lock",
]

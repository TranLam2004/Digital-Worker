"""Lớp 1 — hook tất định, mỗi hook nhỏ, có test riêng. Fail closed ở path_normalize / scope_check / diff_sanity."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from digital_development_team.core.errors import PolicyDenied
from digital_development_team.core.models import PatchBundle
from digital_development_team.util.globs import GlobSet
from digital_development_team.util.paths import UnsafePath, normcase, rel_posix, resolve_inside

# ───── redact_secrets ─────
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("basic", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{12,}={0,2}")),
    ("stripe", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    # user:pass@ trong URL (proxy, DB, registry) — mật khẩu ngắn vẫn là mật khẩu
    ("url_password", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s@]+):[^@/\s]+@")),
    (
        # KEY=giá trị, gồm biến kiểu DB_PASSWORD / AWS_SECRET_ACCESS_KEY / MY_SECRET. Giá trị là chuỗi liền không có
        # ( . [ phía sau: lời gọi hàm, thuộc tính trong code (password = get_password()) không bị che nhầm.
        "kv",
        re.compile(
            r"(?i)\b([a-z0-9_]*?(?:password|passwd|pwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key)"
            r"[a-z0-9_]*)\b(\s*[:=]\s*)['\"]?([A-Za-z0-9_\-+/=!@#%^&*~]{8,}+)(?![(.\[])"
        ),
    ),
)


def _kv_replacer(kind: str) -> Callable[[re.Match[str]], str]:
    def _r(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}[REDACTED:{kind}]"

    return _r


def redact_secrets(text: str) -> tuple[str, int]:
    """Thay secret bằng [REDACTED:<kind>]. Trả (text, số lần)."""
    n = 0
    for kind, pat in _SECRET_PATTERNS:
        if kind == "kv":
            text, k = pat.subn(_kv_replacer(kind), text)
        elif kind == "url_password":  # giữ scheme://user để người đọc biết kết nối tới đâu
            text, k = pat.subn(lambda m: f"{m.group(1)}:[REDACTED:url_password]@", text)
        else:
            text, k = pat.subn(f"[REDACTED:{kind}]", text)
        n += k
    return text, n


# ───── injection_scan ─────
_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore (all |the )?(previous|prior|above) (instructions|prompts?)"),
    re.compile(r"(?i)disregard (all |the )?(previous|prior|above)"),
    re.compile(r"(?i)you are now (in|a|an) "),
    re.compile(r"(?i)^\s*(system|assistant)\s*:", re.M),
    re.compile(r"(?i)\bnew instructions?\b.*\b(approve|push|delete|rm -rf)\b"),
    re.compile(r"(?i)do not (tell|inform) (the )?(user|human)"),
    re.compile(r"(?i)approve (g1|the gate|automatically)"),
)


def injection_scan(text: str) -> list[str]:
    """Pattern khả nghi trong tool result/issue body. Đánh dấu, KHÔNG xóa."""
    found: list[str] = []
    for pat in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            found.append(m.group(0).strip()[:80])
    return found


# ───── path_normalize / scope_check ─────
def path_normalize(worktree: Path, rel: str) -> tuple[Path, str]:
    """(abs, rel_posix) hoặc PolicyDenied nếu thoát worktree / path lạ."""
    try:
        abs_path = resolve_inside(worktree, rel)
    except UnsafePath as e:
        raise PolicyDenied(f"path không an toàn: {e}", rule_id="path_normalize", layer=1) from e
    return abs_path, rel_posix(worktree, abs_path) if abs_path.exists() else rel.replace("\\", "/")


def scope_check(rel: str, allowed_files: Sequence[str]) -> None:
    if not allowed_files:
        raise PolicyDenied(
            "spec.allowed_files rỗng — chưa có spec, không được ghi", rule_id="scope_check", layer=1
        )
    if not GlobSet(allowed_files).match(rel) and normcase(rel) not in {normcase(a) for a in allowed_files}:
        raise PolicyDenied(
            f"{rel} ngoài spec.allowed_files",
            rule_id="scope_violation",
            layer=1,
            hint="Cần sửa file này? Ghi escalation type=ambiguity để Analyst mở rộng spec.",
            allowed=list(allowed_files),
        )


# ───── diff_sanity ─────
_COMMENT_ONLY = re.compile(r"^[+-]\s*(#|//|/\*|\*|<!--|\"\"\"|''')")


def diff_sanity(bundle: PatchBundle, allowed_files: Sequence[str]) -> list[str]:
    """Lỗi cơ học của patch/ (0 LLM): rỗng · chỉ comment/whitespace · file ngoài allowlist. Rỗng = OK."""
    problems: list[str] = []
    if bundle.is_empty:
        problems.append("patch rỗng")
        return problems
    changed = [
        line
        for line in bundle.diff.splitlines()
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---"))
    ]
    if changed and all(not line[1:].strip() or _COMMENT_ONLY.match(line) for line in changed):
        problems.append("diff chỉ đổi comment/whitespace")
    if allowed_files:
        gs = GlobSet(allowed_files)
        allowed_norm = {normcase(a) for a in allowed_files}
        outside = [f for f in bundle.files_touched if not gs.match(f) and normcase(f) not in allowed_norm]
        if outside:
            problems.append(f"đụng file ngoài allowed_files: {outside}")
    return problems

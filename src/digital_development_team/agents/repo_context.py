"""RepoContext — những gì brief cần từ repo/worktree: REPO.md, skeleton, nội dung top file. Không subprocess.

NullRepoContext: chưa có worktree (B9) → ghi chú thay vì nội dung.
FsRepoContext(root): đọc thẳng thư mục (worktree hoặc repo).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.globs import GlobSet
from digital_development_team.util.text import head_tail

_SKIP = GlobSet(
    [
        ".git/",
        ".ddt/",
        "node_modules/",
        "__pycache__/",
        ".venv/",
        "venv/",
        "dist/",
        "build/",
        "*.pyc",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
    ]
)


class RepoContext(Protocol):
    @property
    def root(self) -> Path | None: ...

    def repo_md(self) -> str | None: ...

    def skeleton(self, *, max_entries: int = 200) -> str: ...

    def read_file(self, rel: str, *, max_lines: int = 160) -> str | None: ...

    def read_range(
        self, rel: str, lines: str | None, *, pad: int = 20, max_lines: int = 160
    ) -> str | None: ...


class NullRepoContext:
    root: Path | None = None

    def repo_md(self) -> str | None:
        return None

    def skeleton(self, *, max_entries: int = 200) -> str:
        return "(chưa có worktree — repo context trống)"

    def read_file(self, rel: str, *, max_lines: int = 160) -> str | None:
        return None

    def read_range(self, rel: str, lines: str | None, *, pad: int = 20, max_lines: int = 160) -> str | None:
        return None


class FsRepoContext:
    def __init__(self, root: Path, *, repo_md_path: Path | None = None) -> None:
        self.root: Path | None = root
        self._root = root
        self._repo_md = repo_md_path

    def repo_md(self) -> str | None:
        for p in ([self._repo_md] if self._repo_md else []) + [
            self._root / "REPO.md",
            self._root / ".ddt" / "REPO.md",
        ]:
            if p and p.is_file():
                return p.read_text(encoding=ENCODING)
        return None

    def skeleton(self, *, max_entries: int = 200) -> str:
        entries: list[str] = []
        for p in sorted(self._root.rglob("*")):
            rel = p.relative_to(self._root).as_posix() + ("/" if p.is_dir() else "")
            if _SKIP.match(rel):
                continue
            if p.is_dir():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            entries.append(f"{rel} ({size}B)")
            if len(entries) >= max_entries:
                entries.append(f"… (cắt ở {max_entries} file)")
                break
        return "\n".join(entries) or "(repo trống)"

    def read_file(self, rel: str, *, max_lines: int = 160) -> str | None:
        p = self._root / rel
        if not p.is_file():
            return None
        try:
            text = p.read_text(encoding=ENCODING)
        except (OSError, UnicodeDecodeError):
            return None
        lines = text.splitlines()
        return chr(10).join(head_tail(lines, head=max_lines * 2 // 3, tail=max_lines // 3))

    def read_range(self, rel: str, lines: str | None, *, pad: int = 20, max_lines: int = 160) -> str | None:
        """Đoạn code quanh `lines` ("42-58" | "42") ±pad dòng, CÓ số dòng — Dev sửa ngay, khỏi fs.read lại.
        File ngắn (≤ max_lines) → cả file. Không có/không đọc được → None."""
        p = self._root / rel
        if not p.is_file():
            return None
        try:
            all_lines = p.read_text(encoding=ENCODING).splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        n = len(all_lines)
        span = parse_span(lines)
        if n <= max_lines or span is None:
            lo, hi = 1, min(n, max_lines)
        else:
            lo, hi = max(1, span[0] - pad), min(n, span[1] + pad)
            hi = min(hi, lo + max_lines - 1)
        body = chr(10).join(f"{i:>5}  {all_lines[i - 1]}" for i in range(lo, hi + 1))
        tail = f"{chr(10)}… ({n - hi} more lines)" if hi < n else ""
        head = f"… ({lo - 1} lines above){chr(10)}" if lo > 1 else ""
        return head + body + tail


def parse_span(lines: str | None) -> tuple[int, int] | None:
    """ "42-58" → (42, 58) · "42" → (42, 42) · khác → None."""
    if not lines:
        return None
    m = re.match(r"\s*(\d+)\s*(?:[-\u2013:]\s*(\d+))?", str(lines))  # 42-58 · 42 en-dash 58 · 42:58
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return (min(a, b), max(a, b))


def pick_top_ranges(candidates: Sequence[dict[str, object]], limit: int = 3) -> list[tuple[str, str | None]]:
    """plan.candidates → (đường dẫn, dòng) của candidate #1..#limit (không trùng file)."""
    out: list[tuple[str, str | None]] = []
    for c in candidates:
        files = c.get("files") if isinstance(c, dict) else None
        if not isinstance(files, list):
            continue
        for f in files:
            path = f.get("path") if isinstance(f, dict) else None
            if isinstance(path, str) and path not in [p for p, _ in out]:
                lines = f.get("lines") if isinstance(f, dict) else None
                out.append((path, str(lines) if lines else None))
            if len(out) >= limit:
                return out
    return out


def pick_top_files(candidates: Sequence[dict[str, object]], limit: int = 3) -> list[str]:
    """plan.candidates → đường dẫn file của candidate #1..#limit (không trùng)."""
    out: list[str] = []
    for c in candidates:
        files = c.get("files") if isinstance(c, dict) else None
        if not isinstance(files, list):
            continue
        for f in files:
            path = f.get("path") if isinstance(f, dict) else None
            if isinstance(path, str) and path not in out:
                out.append(path)
            if len(out) >= limit:
                return out
    return out

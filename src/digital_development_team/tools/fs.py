"""fs.* — list · read · search · write · edit · delete. Path qua resolve_inside(worktree). Giữ EOL gốc khi edit."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from digital_development_team.config.models import CommandGuardCfg
from digital_development_team.core.context import CancelToken, ToolContext
from digital_development_team.core.models import ToolResult
from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.base import Handler
from digital_development_team.util.fsio import (
    ENCODING,
    apply_eol,
    detect_eol,
    normalize_eol,
    write_text_atomic,
)
from digital_development_team.util.globs import GlobSet
from digital_development_team.util.paths import UnsafePath, rel_posix, resolve_inside
from digital_development_team.util.proc import run_process

_MAX_BYTES = 5 * 1024 * 1024
_FULL_READ_LINES = 300
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
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "*.pyc",
    ]
)
_SYMBOL_RE = re.compile(
    r"^\s*(?:async\s+def|def|class|function|fn|func|pub fn|export (?:default )?(?:async )?function|interface|struct|type)\s+[A-Za-z_][\w]*[^\n]{0,80}",
    re.M,
)
_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
}


def _wt(ctx: ToolContext) -> Path:
    if not ctx.worktree:
        raise UnsafePath("chưa có worktree")
    return Path(ctx.worktree)


def _resolve(ctx: ToolContext, rel: str) -> tuple[Path, str]:
    wt = _wt(ctx)
    p = resolve_inside(wt, rel)
    return p, rel_posix(wt, p) if p.exists() else rel.replace("\\", "/")


_SECRET_GLOBS = tuple(CommandGuardCfg().secret_globs)
_SECRET_SET = GlobSet(_SECRET_GLOBS)


def _is_secret(rel: str) -> bool:
    parts = [x for x in rel.replace(chr(92), "/").split("/") if x]
    return any(_SECRET_SET.match(x) for x in parts)


_REGEX_HINTS = re.compile(r"\||\.[*+]|\\[bdwsBDWS]|\[[^\]]+\]|\(\?|^\^|\$$")


def _looks_like_regex(pattern: str) -> bool:
    """Chuỗi có cú pháp regex rõ ràng (`|`, `.*`, `\\b`, `[...]`, `^…`, `…$`) và biên dịch được."""
    if not _REGEX_HINTS.search(pattern):
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


class FsTools:
    def __init__(self, artifacts: ArtifactStore, *, rg: str | None = None) -> None:
        self._artifacts = artifacts
        self._rg = rg if rg is not None else shutil.which("rg")

    def handlers(self) -> dict[str, Handler]:
        return {
            "list": self.list_dir,
            "read": self.read,
            "search": self.search,
            "write": self.write,
            "edit": self.edit,
            "delete": self.delete,
        }

    # ───── list ─────
    def list_dir(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        rel = str(args.get("path") or ".")
        depth = int(args.get("depth") or 2)
        skeleton = bool(args.get("skeleton", False))
        try:
            root, _ = _resolve(ctx, rel)
        except UnsafePath as e:
            return ToolResult.fail("NOT_FOUND", str(e), hint="path tương đối worktree")
        if not root.is_dir():
            return ToolResult.fail("NOT_FOUND", f"{rel} không phải thư mục", hint="Dùng fs.search để tìm")
        wt = _wt(ctx)
        entries: list[dict[str, Any]] = []
        dirs = 0
        truncated = False
        for p in sorted(root.rglob("*")):
            r = rel_posix(wt, p) + ("/" if p.is_dir() else "")
            if _SKIP.match(r):
                continue
            if len(Path(r).parts) - len(Path(rel_posix(wt, root)).parts if rel != "." else ()) > depth:
                continue
            if p.is_dir():
                dirs += 1
                continue
            try:
                text = p.read_text(encoding=ENCODING) if p.stat().st_size <= _MAX_BYTES else None
            except (OSError, UnicodeDecodeError):
                text = None
            entry: dict[str, Any] = {
                "path": r,
                "type": "file",
                "lines": text.count("\n") + 1 if text else None,
                "lang": _LANG.get(p.suffix.lower()),
            }
            if skeleton and text and entry["lang"] not in (None, "markdown", "yaml", "json", "toml"):
                entry["symbols"] = [m.group(0).strip().rstrip(":{") for m in _SYMBOL_RE.finditer(text)][:40]
            entries.append(entry)
            if len(entries) >= 200:
                truncated = True
                break
        return ToolResult.ok(
            f"list {rel} depth={depth} → {len(entries)} files, {dirs} dirs" + (" (cắt)" if truncated else ""),
            entries=entries,
            truncated=truncated,
        )

    # ───── read ─────
    def read(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        rel = str(args.get("path", ""))
        if rel.startswith("artifact://"):
            text = self._artifacts.read(rel)
            if text is None:
                return ToolResult.fail("NOT_FOUND", f"artifact {rel} không có")
            return self._read_text(rel, text, args.get("lines"), ctx)
        try:
            p, r = _resolve(ctx, rel)
        except UnsafePath as e:
            return ToolResult.fail("NOT_FOUND", str(e))
        if not p.is_file():
            return ToolResult.fail(
                "NOT_FOUND", f"{rel} không tồn tại", hint="Dùng fs.search hoặc fs.list để tìm"
            )
        if p.stat().st_size > _MAX_BYTES:
            return ToolResult.fail(
                "TOO_LARGE", f"{rel} > 5MB", hint="File binary hoặc generated — có chắc cần đọc?"
            )
        try:
            text = p.read_text(encoding=ENCODING)
        except UnicodeDecodeError:
            return ToolResult.fail("TOO_LARGE", f"{rel} không phải UTF-8 text", hint="File binary?")
        return self._read_text(r, text, args.get("lines"), ctx)

    def _read_text(self, rel: str, text: str, lines: Any, ctx: ToolContext) -> ToolResult:
        all_lines = text.splitlines()
        total = len(all_lines)
        lang = _LANG.get(Path(rel).suffix.lower())
        if lines:
            a, b = (int(x) for x in str(lines).split("-", 1))
            lo, hi = max(1, a - 5), min(total, b + 5)
            chunk = all_lines[lo - 1 : hi]
            content = "\n".join(f"{i:>5} {line}" for i, line in zip(range(lo, hi + 1), chunk, strict=False))
            return ToolResult.ok(
                f"read {rel}:{a}-{b} → {len(chunk)} lines",
                path=rel,
                lines_total=total,
                lines_returned=len(chunk),
                range=f"{lo}-{hi}",
                lang=lang,
                content=content,
            )
        if total > _FULL_READ_LINES:
            ref = self._artifacts.put(
                ctx.issue_id, text, ext=Path(rel).suffix.lstrip(".") or "txt", label=Path(rel).stem
            )
            return ToolResult(
                status="ok",
                summary=f"read {rel} → {total} lines (lớn, lưu artifact; dùng lines='a-b' để đọc dải)",
                structured={
                    "path": rel,
                    "lines_total": total,
                    "lines_returned": 20,
                    "lang": lang,
                    "artifact": ref,
                },
                artifact_refs=(ref,),
                preview=tuple(all_lines[:20]),
            )
        return ToolResult.ok(
            f"read {rel} → {total} lines",
            path=rel,
            lines_total=total,
            lines_returned=total,
            lang=lang,
            content=text,
        )

    # ───── search ─────
    def search(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        rel = str(args.get("path") or ".")
        regex = bool(args.get("regex", False))
        limit = int(args.get("max") or 50)
        try:
            root, _ = _resolve(ctx, rel)
        except UnsafePath as e:
            return ToolResult.fail("NOT_FOUND", str(e))
        wt = _wt(ctx)
        matches = self._run_search(pattern, root, wt, regex, limit, ctx.cancel)
        as_regex = False
        if not matches and not regex and _looks_like_regex(pattern):
            # agent hay gửi "a|b" mà quên regex=true (live DDT-0003: 0 match, mất lượt duy nhất) → thử lại dạng regex
            matches, as_regex = self._run_search(pattern, root, wt, True, limit, ctx.cancel), True
        files = {m["path"] for m in matches}
        truncated = len(matches) >= limit
        return ToolResult.ok(
            f"search '{pattern[:40]}'"
            + (" (0 literal matches → retried as regex)" if as_regex else "")
            + f" → {len(matches)} matches in {len(files)} files"
            + (" (cắt)" if truncated else ""),
            matches=matches,
            count=len(matches),
            truncated=truncated,
            **({"regex": True} if as_regex else {}),
        )

    def _run_search(
        self, pattern: str, root: Path, wt: Path, regex: bool, limit: int, cancel: CancelToken
    ) -> list[dict[str, Any]]:
        matches = self._rg_search(pattern, root, wt, regex, limit, cancel) if self._rg else None
        if matches is None:
            matches = self._py_search(pattern, root, wt, regex, limit)
        return [m for m in matches if not _is_secret(str(m["path"]))]

    def _rg_search(
        self, pattern: str, root: Path, wt: Path, regex: bool, limit: int, cancel: CancelToken
    ) -> list[dict[str, Any]] | None:
        import json

        out = wt.parent / f".ddt-rg-{abs(hash((pattern, str(root))))}.out"
        err = out.with_suffix(".err")
        argv = [
            self._rg or "rg",
            "--json",
            "-m",
            str(limit),
            "--no-config",
            "-g",
            "!.git",
            "-g",
            "!node_modules",
            "-g",
            "!__pycache__",
        ]
        for g in _SECRET_GLOBS:  # không đọc nội dung file bí mật, kể cả khi tìm cả repo
            argv += ["-g", f"!{g}"]
        if not regex:
            argv.append("-F")
        argv += ["--", pattern, str(root)]
        try:
            res = run_process(
                argv,
                cwd=wt,
                env={"PATH": os.environ.get("PATH", "")},
                timeout_s=10,
                stdout_path=out,
                stderr_path=err,
                cancel=cancel,
            )
            if res.exit_code not in (0, 1):
                return None
            text = out.read_text(encoding=ENCODING, errors="replace") if out.exists() else ""
        finally:
            for p in (out, err):
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
        matches: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "match":
                continue
            d = obj["data"]
            path = d["path"].get("text", "")
            try:
                rp = rel_posix(wt, Path(path))
            except ValueError:
                rp = path.replace("\\", "/")
            matches.append(
                {"path": rp, "line": d["line_number"], "text": d["lines"].get("text", "").rstrip()[:200]}
            )
            if len(matches) >= limit:
                break
        return matches

    @staticmethod
    def _py_search(pattern: str, root: Path, wt: Path, regex: bool, limit: int) -> list[dict[str, Any]]:
        rx = re.compile(pattern if regex else re.escape(pattern))
        matches: list[dict[str, Any]] = []
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for p in files:
            r = rel_posix(wt, p)
            if _SKIP.match(r) or _is_secret(r):
                continue
            try:
                for i, line in enumerate(p.read_text(encoding=ENCODING).splitlines(), start=1):
                    if rx.search(line):
                        matches.append({"path": r, "line": i, "text": line.rstrip()[:200]})
                        if len(matches) >= limit:
                            return matches
            except (OSError, UnicodeDecodeError):
                continue
        return matches

    # ───── write / edit / delete ─────
    def write(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        rel, content = str(args.get("path", "")), str(args.get("content", ""))
        try:
            p, r = _resolve(ctx, rel)
        except UnsafePath as e:
            return ToolResult.fail("NOT_FOUND", str(e))
        created = not p.exists()
        eol = detect_eol(p.read_text(encoding=ENCODING)) if not created else "\n"
        write_text_atomic(p, apply_eol(normalize_eol(content), eol), fsync=False)
        return ToolResult.ok(
            f"write {r} → {len(content.encode('utf-8'))} bytes ({'created' if created else 'overwritten'})",
            path=r,
            bytes=len(content.encode("utf-8")),
            created=created,
        )

    def edit(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        rel, old, new = str(args.get("path", "")), str(args.get("old", "")), str(args.get("new", ""))
        replace_all = bool(args.get("all", False))
        try:
            p, r = _resolve(ctx, rel)
        except UnsafePath as e:
            return ToolResult.fail("NOT_FOUND", str(e))
        if not p.is_file():
            return ToolResult.fail("NOT_FOUND", f"{rel} không tồn tại", hint="fs.write để tạo mới")
        raw = p.read_text(encoding=ENCODING)
        eol = detect_eol(raw)
        text = normalize_eol(raw)
        old_n, new_n = normalize_eol(old), normalize_eol(new)
        count = text.count(old_n)
        if count == 0:
            return ToolResult.fail(
                "EDIT_NO_MATCH",
                f"`old` không có trong {r}",
                hint="Đọc lại file (fs.read), copy chính xác đoạn cần sửa",
            )
        if count > 1 and not replace_all:
            return ToolResult.fail(
                "EDIT_AMBIGUOUS",
                f"`old` xuất hiện {count} lần trong {r}",
                hint="Thêm context để `old` duy nhất, hoặc all=true",
            )
        line = text[: text.index(old_n)].count("\n") + 1
        updated = text.replace(old_n, new_n) if replace_all else text.replace(old_n, new_n, 1)
        write_text_atomic(p, apply_eol(updated, eol), fsync=False)
        n = count if replace_all else 1
        return ToolResult.ok(
            f"edit {r}:{line} → {n} replacement{'s' if n > 1 else ''}", path=r, replacements=n, line=line
        )

    def delete(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        # Policy G2 chặn trước khi tới đây; nếu tới → đã được người duyệt (P2).
        rel = str(args.get("path", ""))
        try:
            p, r = _resolve(ctx, rel)
        except UnsafePath as e:
            return ToolResult.fail("NOT_FOUND", str(e))
        if not p.is_file():
            return ToolResult.fail("NOT_FOUND", f"{rel} không tồn tại")
        p.unlink()
        return ToolResult.ok(f"delete {r}", path=r)

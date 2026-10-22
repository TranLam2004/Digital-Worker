"""codegraph.* — backend CodeGraph CLI (`codegraph <cmd> --json -p <repo>`, npm @colbymchenry/codegraph, MIT, tree-sitter)
với **fallback PyGraph** (tools/pygraph.py, stdlib ast) khi thiếu binary hoặc chưa index → Python repo dùng ngay, 0 cài thêm.

Index nằm ở REPO CHÍNH (`<repo>/.codegraph/`, exclude qua .git/info/exclude), không index worktree:
  • init lúc onboarding / `ddt debug codegraph-init`; `sync` best-effort sau MERGE.
  • Gọi từ IMPLEMENT/VERIFY (worktree khác repo) → kèm `stale: true` + danh sách file đã đổi trong patch (index chưa thấy).
Lỗi theo contract 40-tools/codegraph.tool.md: NOT_AVAILABLE (thiếu binary) · NOT_INDEXED · SYMBOL_NOT_FOUND · AMBIGUOUS
— hint luôn trỏ về fs.search để agent không kẹt. Mọi lệnh qua sandbox profile verifier (chỉ đọc).
"""

from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import CancelToken, ToolContext
from digital_development_team.core.errors import IsolationError, SandboxError
from digital_development_team.core.models import ToolResult
from digital_development_team.core.protocols import Sandbox
from digital_development_team.isolation.git import Git
from digital_development_team.risk.blast import impact_risk
from digital_development_team.sandbox.profiles import profile_for
from digital_development_team.tools.base import Handler
from digital_development_team.tools.exec import read_output
from digital_development_team.tools.pygraph import pygraph_for
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.testpaths import is_test_path

__all__ = ["is_test_path"]

BINARY = "codegraph"
INDEX_DIR = ".codegraph"
FALLBACK_HINT = "Dùng fs.search / fs.list(skeleton=true) thay thế."


def cg_available() -> bool:
    return shutil.which(BINARY) is not None


def is_indexed(repo: Path) -> bool:
    return (repo / INDEX_DIR / "codegraph.db").is_file()


def _parse_json(text: str) -> Any:
    t = text.strip()
    for opener in ("{", "["):
        i = t.find(opener)
        if i >= 0:
            try:
                return json.loads(t[i:])
            except json.JSONDecodeError:
                continue
    return None


def run_cli(
    sandbox: Sandbox,
    cfg: SandboxCfg,
    repo: Path,
    args: Sequence[str],
    cancel: CancelToken,
    *,
    timeout_s: float = 15.0,
    profile: str = "verifier",
    positional_path: bool = False,
) -> tuple[Any, str, int, bool]:
    """→ (json | None, stderr, exit_code, timed_out). cwd=repo chính; query/callers/impact nhận `-p <repo>`,
    status/init/sync nhận path vị trí (positional_path=True)."""
    prof = profile_for(cfg, profile, repo)
    rp = shlex.quote(str(repo).replace("\\", "/"))
    cmd = " ".join([BINARY, *(shlex.quote(a) for a in args), *([rp] if positional_path else ["-p", rp])])
    res = sandbox.run(cmd, cwd=repo, profile=prof, timeout_s=timeout_s, cancel=cancel)
    out, err = read_output(res)
    return _parse_json(out), err, res.exit_code, res.timed_out


def exclude_index_from_git(repo: Path) -> None:
    """Ghi `.codegraph/` vào .git/info/exclude — không đụng .gitignore của repo đích."""
    info = repo / ".git" / "info"
    if not (repo / ".git").exists():
        return
    info.mkdir(parents=True, exist_ok=True)
    ex = info / "exclude"
    cur = ex.read_text(encoding=ENCODING) if ex.is_file() else ""
    if f"{INDEX_DIR}/" not in cur.splitlines():
        ex.write_text(cur.rstrip("\n") + ("\n" if cur else "") + f"{INDEX_DIR}/\n", encoding=ENCODING)


def ensure_index(
    sandbox: Sandbox, cfg: SandboxCfg, repo: Path, cancel: CancelToken, *, timeout_s: float = 600.0
) -> dict[str, Any]:
    """`codegraph init <repo>` nếu chưa có index (profile dev vì ghi .codegraph/). → {status, summary, nodes?}."""
    if not cg_available():
        return {
            "status": "skipped",
            "summary": "codegraph không có trong PATH (npm i -g @colbymchenry/codegraph)",
        }
    if is_indexed(repo):
        exclude_index_from_git(repo)
        return {"status": "ok", "summary": "đã có index"}
    prof = profile_for(cfg, "dev", repo)
    res = sandbox.run(
        f"{BINARY} init {shlex.quote(str(repo).replace(chr(92), '/'))}",
        cwd=repo,
        profile=prof,
        timeout_s=timeout_s,
        cancel=cancel,
    )
    out, err = read_output(res)
    if res.timed_out or not is_indexed(repo):
        return {
            "status": "fail",
            "summary": f"codegraph init thất bại (exit {res.exit_code}): {(err or out).strip()[-200:]}",
        }
    exclude_index_from_git(repo)
    return {"status": "ok", "summary": "đã index", **status_of(sandbox, cfg, repo, cancel)}


def status_of(sandbox: Sandbox, cfg: SandboxCfg, repo: Path, cancel: CancelToken) -> dict[str, Any]:
    if not cg_available() or not is_indexed(repo):
        return {"indexed": False}
    d, _err, _code, _to = run_cli(
        sandbox, cfg, repo, ["status", "-j"], cancel, timeout_s=30.0, positional_path=True
    )
    if not isinstance(d, dict):
        return {"indexed": True}
    pend = d.get("pendingChanges") or {}
    return {
        "indexed": bool(d.get("initialized", True)),
        "nodes": d.get("nodeCount"),
        "files": d.get("fileCount"),
        "pending": sum(int(pend.get(k) or 0) for k in ("added", "modified", "removed")),
        "languages": d.get("languages"),
    }


def sync_index(
    sandbox: Sandbox, cfg: SandboxCfg, repo: Path, cancel: CancelToken, *, timeout_s: float = 120.0
) -> dict[str, Any]:
    if not cg_available() or not is_indexed(repo):
        return {"status": "skipped", "summary": "chưa có index"}
    prof = profile_for(cfg, "dev", repo)
    res = sandbox.run(
        f"{BINARY} sync -q {shlex.quote(str(repo).replace(chr(92), '/'))}",
        cwd=repo,
        profile=prof,
        timeout_s=timeout_s,
        cancel=cancel,
    )
    return {
        "status": "ok" if res.exit_code == 0 and not res.timed_out else "fail",
        "summary": f"codegraph sync exit {res.exit_code}",
    }


def _node(n: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": n.get("name"),
        "kind": n.get("kind"),
        "path": str(n.get("filePath") or "").replace("\\", "/"),
        "line": n.get("startLine"),
        "end_line": n.get("endLine"),
        "signature": n.get("signature"),
        "qualified_name": n.get("qualifiedName"),
    }


class CodeGraphTools:
    """Handler codegraph.* + API nội bộ (impact_of) cho blast radius của MACHINE_GATE."""

    def __init__(
        self,
        sandbox: Sandbox,
        cfg: SandboxCfg,
        repo: Path,
        *,
        git: Git | None = None,
        base_ref: Callable[[ToolContext], str] | None = None,
        thresholds: Mapping[str, int] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._cfg = cfg
        self._repo = repo
        self._git = git
        self._base_ref = base_ref or (lambda _ctx: "main")
        th = dict(thresholds or {})
        self._low, self._medium = int(th.get("low", 3)), int(th.get("medium", 8))

    def handlers(self) -> dict[str, Handler]:
        return {
            "search": self.search,
            "callers": self.callers,
            "callees": self.callees,
            "impact": self.impact,
            "node": self.node,
        }

    # ───── backend ─────
    def use_cli(self) -> bool:
        return cg_available() and is_indexed(self._repo)

    def _guard(self, ctx: ToolContext) -> ToolResult | None:
        """CLI thiếu/chưa index → không lỗi: rẽ sang PyGraph (ast) trên worktree hiện tại."""
        return None

    def _root(self, ctx: ToolContext) -> Path:
        return Path(ctx.worktree) if ctx.worktree else self._repo

    def _py(self, ctx: ToolContext) -> Any:
        return pygraph_for(self._root(ctx))

    def _stale(self, ctx: ToolContext) -> list[str]:
        """File đã đổi trong worktree so với base → index (repo chính) chưa thấy."""
        if self._git is None or not ctx.worktree or Path(ctx.worktree).resolve() == self._repo.resolve():
            return []
        wt = Path(ctx.worktree)
        try:
            return sorted(
                {p for _, p in self._git.status_porcelain(cwd=wt)}
                | set(self._git.changed_files(cwd=wt, base=self._base_ref(ctx)))
            )
        except IsolationError:
            return []

    def _cli(self, ctx: ToolContext, args: Sequence[str], timeout_s: float) -> tuple[Any, ToolResult | None]:
        try:
            d, err, code, timed_out = run_cli(
                self._sandbox, self._cfg, self._repo, args, ctx.cancel, timeout_s=timeout_s
            )
        except SandboxError as e:
            return None, ToolResult.fail("SANDBOX", e.message, hint=e.hint)
        if timed_out:
            return None, ToolResult.fail(
                "TIMEOUT", f"codegraph {args[0]} quá {int(timeout_s)}s", hint=FALLBACK_HINT
            )
        if d is None:
            return None, ToolResult.fail(
                "CODEGRAPH", f"codegraph {args[0]} exit {code}: {err.strip()[-200:]}", hint=FALLBACK_HINT
            )
        return d, None

    def _with_stale(self, ctx: ToolContext, structured: dict[str, Any]) -> dict[str, Any]:
        stale = self._stale(ctx)
        if stale:
            structured["stale"] = True
            structured["stale_files"] = stale[:20]
            structured["stale_hint"] = (
                "index theo repo chính; file trên đã đổi trong patch — xác nhận bằng fs.read/fs.search"
            )
        return structured

    # ───── ops ─────
    def search(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if (g := self._guard(ctx)) is not None:
            return g
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.fail("BAD_ARGS", "query rỗng")
        limit = int(args.get("limit") or 10)
        kind = str(args["kind"]) if args.get("kind") else None
        if not self.use_cli():
            hits = self._py(ctx).search(query, kind=kind, limit=limit)
            if not hits:
                return ToolResult.fail(
                    "SYMBOL_NOT_FOUND", f"không có symbol khớp {query!r}", hint=FALLBACK_HINT
                )
            return ToolResult.ok(
                f"codegraph.search {query!r} → {len(hits)} symbol (pyast)",
                symbols=hits,
                count=len(hits),
                backend="pyast",
            )
        cli = ["query", query, "-j", "-l", str(limit)]
        if kind:
            cli += ["-k", kind]
        d, err = self._cli(ctx, cli, 15.0)
        if err:
            return err
        hits = [
            {**_node(h.get("node") or {}), "score": round(float(h.get("score") or 0), 1)}
            for h in (d if isinstance(d, list) else [])
        ]
        if not hits:
            return ToolResult.fail("SYMBOL_NOT_FOUND", f"không có symbol khớp {query!r}", hint=FALLBACK_HINT)
        return ToolResult.ok(
            f"codegraph.search {query!r} → {len(hits)} symbol",
            **self._with_stale(ctx, {"symbols": hits, "count": len(hits)}),
        )

    def _edges(self, ctx: ToolContext, op: str, args: Mapping[str, Any], key: str) -> ToolResult:
        if (g := self._guard(ctx)) is not None:
            return g
        symbol = str(args.get("symbol", "")).strip()
        if not symbol:
            return ToolResult.fail("BAD_ARGS", "symbol rỗng")
        limit = int(args.get("limit") or 20)
        if not self.use_cli():
            g = self._py(ctx)
            rows = g.callers(symbol, limit=limit) if op == "callers" else g.callees(symbol, limit=limit)
            if not g.exact(symbol):
                return ToolResult.fail("SYMBOL_NOT_FOUND", f"không có symbol {symbol!r}", hint=FALLBACK_HINT)
            tests = [r for r in rows if is_test_path(str(r["path"]))]
            return ToolResult.ok(
                f"codegraph.{op} {symbol!r} → {len(rows)} ({len(tests)} test, pyast)",
                **{
                    "symbol": symbol,
                    key: rows,
                    "count": len(rows),
                    "test_count": len(tests),
                    "files": sorted({str(r["path"]) for r in rows}),
                    "backend": "pyast",
                },
            )
        d, err = self._cli(ctx, [op, symbol, "-j", "-l", str(limit)], 15.0)
        if err:
            return err
        rows = [_node(r) for r in (d.get(key) or [])] if isinstance(d, dict) else []
        if isinstance(d, dict) and d.get("error"):
            return ToolResult.fail("SYMBOL_NOT_FOUND", str(d["error"])[:200], hint=FALLBACK_HINT)
        tests = [r for r in rows if is_test_path(str(r["path"]))]
        return ToolResult.ok(
            f"codegraph.{op} {symbol!r} → {len(rows)} ({len(tests)} test)",
            **self._with_stale(
                ctx,
                {
                    "symbol": symbol,
                    key: rows,
                    "count": len(rows),
                    "test_count": len(tests),
                    "files": sorted({str(r["path"]) for r in rows}),
                },
            ),
        )

    def callers(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        return self._edges(ctx, "callers", args, "callers")

    def callees(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        return self._edges(ctx, "callees", args, "callees")

    def node(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if (g := self._guard(ctx)) is not None:
            return g
        symbol = str(args.get("symbol", "")).strip()
        if not symbol:
            return ToolResult.fail("BAD_ARGS", "symbol rỗng")
        if not self.use_cli():
            g = self._py(ctx)
            found = g.exact(symbol, str(args["path"]) if args.get("path") else None)
            if not found:
                return ToolResult.fail(
                    "SYMBOL_NOT_FOUND", f"không có symbol tên đúng {symbol!r}", hint=FALLBACK_HINT
                )
            if len(found) > 1:
                return ToolResult.fail(
                    "AMBIGUOUS",
                    f"{len(found)} symbol cùng tên {symbol!r}",
                    hint="truyền path để chọn",
                    candidates=[{"path": s.path, "line": s.line, "kind": s.kind} for s in found],
                )
            s0 = found[0]
            return ToolResult.ok(
                f"codegraph.node {symbol!r} {s0.path}:{s0.line} (pyast)",
                **s0.to_dict(),
                source=g.source(s0),
                backend="pyast",
            )
        d, err = self._cli(ctx, ["query", symbol, "-j", "-l", "10"], 10.0)
        if err:
            return err
        exact = [
            _node(h.get("node") or {})
            for h in (d if isinstance(d, list) else [])
            if (h.get("node") or {}).get("name") == symbol
        ]
        if args.get("path"):
            want = str(args["path"]).replace("\\", "/")
            exact = [n for n in exact if str(n["path"]).endswith(want)]
        if not exact:
            return ToolResult.fail(
                "SYMBOL_NOT_FOUND", f"không có symbol tên đúng {symbol!r}", hint=FALLBACK_HINT
            )
        if len(exact) > 1:
            return ToolResult.fail(
                "AMBIGUOUS",
                f"{len(exact)} symbol cùng tên {symbol!r}",
                hint="truyền path để chọn",
                candidates=[{"path": n["path"], "line": n["line"], "kind": n["kind"]} for n in exact],
            )
        n = exact[0]
        src = self._read_lines(ctx, n)
        return ToolResult.ok(
            f"codegraph.node {symbol!r} {n['path']}:{n['line']}",
            **self._with_stale(ctx, {**n, "source": src}),
        )

    def _read_lines(self, ctx: ToolContext, n: Mapping[str, Any]) -> str | None:
        root = Path(ctx.worktree) if ctx.worktree else self._repo
        p = root / str(n["path"])
        if not p.is_file() or not n.get("line"):
            return None
        lines = p.read_text(encoding=ENCODING, errors="replace").splitlines()
        a = max(int(n["line"]) - 1, 0)
        b = int(n.get("end_line") or (a + 40))
        return "\n".join(lines[a : min(b, a + 120)])

    def impact(self, args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
        if (g := self._guard(ctx)) is not None:
            return g
        symbol = str(args.get("symbol", "")).strip()
        if not symbol:
            return ToolResult.fail("BAD_ARGS", "symbol rỗng")
        r = self.impact_of(symbol, ctx.cancel, depth=int(args.get("depth") or 2))
        if r is None:
            return ToolResult.fail(
                "SYMBOL_NOT_FOUND", f"không tính được impact cho {symbol!r}", hint=FALLBACK_HINT
            )
        return ToolResult.ok(
            f"codegraph.impact {symbol!r} → {len(r['affected_files'])} file · {len(r['affected_tests'])} test · risk {r['risk']}",
            **self._with_stale(ctx, r),
        )

    def impact_of(self, symbol: str, cancel: CancelToken, *, depth: int = 2) -> dict[str, Any] | None:
        """Chuẩn hóa `codegraph impact` về contract: {symbol, depth, affected_symbols[], affected_files[], affected_tests[], risk}.
        Bỏ node kind=file và chính symbol; test = path khớp pattern test. Không có CLI/index → PyGraph trên repo chính."""
        if not self.use_cli():
            return self._impact_py(symbol, depth=depth)
        try:
            d, _err, _code, timed_out = run_cli(
                self._sandbox,
                self._cfg,
                self._repo,
                ["impact", symbol, "-j", "-d", str(depth)],
                cancel,
                timeout_s=20.0,
            )
        except SandboxError:
            return None
        if timed_out or not isinstance(d, dict) or d.get("error"):
            return None
        rows = [
            _node(r) for r in (d.get("affected") or []) if r.get("kind") != "file" and r.get("name") != symbol
        ]
        files = sorted({str(r["path"]) for r in rows if r["path"]})
        tests = sorted({str(r["path"]) for r in rows if is_test_path(str(r["path"]))})
        src_files = [f for f in files if not is_test_path(f)]
        public = any(f.endswith("__init__.py") or "/api/" in f or f.startswith("api/") for f in src_files)
        return {
            "symbol": symbol,
            "depth": int(d.get("depth") or depth),
            "affected_symbols": [f"{r['path']}::{r['name']}" for r in rows][:30],
            "affected_files": files,
            "affected_tests": tests,
            "files": [{"path": f, "is_test": is_test_path(f)} for f in files],
            "risk": impact_risk(
                len(src_files), touches_public_api=public, low=self._low, medium=self._medium
            ),
        }

    def _impact_py(self, symbol: str, *, depth: int, root: Path | None = None) -> dict[str, Any] | None:
        g = pygraph_for(root or self._repo)
        if not g.exact(symbol):
            return None
        r = g.impact(symbol, depth=depth)
        files = [f for f in r["files"]]
        src_files = [f for f in files if not is_test_path(f)]
        public = any(f.endswith("__init__.py") or "/api/" in f or f.startswith("api/") for f in src_files)
        return {
            "symbol": symbol,
            "depth": depth,
            "depth_reached": r["depth_reached"],
            "truncated": r["truncated"],
            "affected_symbols": [f"{a['path']}::{a['qualified_name']}" for a in r["affected"]][:30],
            "affected_files": files,
            "affected_tests": r["tests"],
            "files": [{"path": f, "is_test": is_test_path(f)} for f in files],
            "risk": impact_risk(
                len(src_files), touches_public_api=public, low=self._low, medium=self._medium
            ),
            "backend": "pyast",
        }

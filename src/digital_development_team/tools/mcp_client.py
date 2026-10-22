"""MCP client (Phase 10): server ngoài khai ở policy.yaml#mcp_client → tool `mcp.<server>__<tool>`.

- Tool đi qua đúng Dispatcher/policy/event như tool nội bộ (risk tier do người khai, không có destructive), output đi
  qua quét injection như mọi nguồn ngoài (policy engine: tiền tố `mcp.`).
- Catalog dựng từ snapshot `<home>/state/mcp/<server>.json` (`ddt ext mcp sync`), KHÔNG khởi động server lúc dựng.
  Tool chỉ lộ ra khi snapshot khớp `pinned_sha`; lúc kết nối thật, schema server trả về lệch pin → từ chối gọi.
- Kết nối lười, một kết nối mỗi server trong process, khoá theo server (sub-agent gọi song song an toàn).
- stdio: JSON-RPC theo dòng qua util/proc.PipeProcess, env chỉ gồm biến khai báo + vài biến hệ thống tối thiểu.
  http (streamable): POST JSON-RPC; URL phải qua policy.network.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from digital_development_team import __version__
from digital_development_team.config.models import McpClientCfg, McpServerCfg, NetworkCfg
from digital_development_team.core.context import ToolContext
from digital_development_team.core.enums import RiskTier
from digital_development_team.core.models import ToolResult
from digital_development_team.policy.netguard import check_url
from digital_development_team.tools.base import Handler
from digital_development_team.tools.catalog import ToolOp
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.proc import PipeProcess

log = logging.getLogger(__name__)

PROTOCOL = "2025-06-18"
SNAP_DIR = "mcp"
_SLUG = re.compile(r"[^a-z0-9_]+")
_SYS_ENV = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
MAX_TEXT = 200_000


class McpError(Exception):
    pass


def op_name(server: str, tool: str) -> str:
    slug = _SLUG.sub("_", tool.lower()).strip("_") or "tool"
    return f"mcp.{server}__{slug}"[:64]  # wire mcp_<server>__<tool> ≤ 64 ký tự


def schema_sha(tools: Iterable[Mapping[str, Any]]) -> str:
    norm = sorted(
        (
            {"name": t.get("name"), "description": t.get("description"), "inputSchema": t.get("inputSchema")}
            for t in tools
        ),
        key=lambda t: str(t["name"]),
    )
    return hashlib.sha256(json.dumps(norm, sort_keys=True, ensure_ascii=False).encode(ENCODING)).hexdigest()


def snapshot_path(state_dir: Path, server: str) -> Path:
    return state_dir / SNAP_DIR / f"{server}.json"


def read_snapshot(state_dir: Path, server: str) -> dict[str, Any] | None:
    p = snapshot_path(state_dir, server)
    try:
        data = json.loads(p.read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def snapshot_ops(cfg: McpClientCfg, state_dir: Path) -> tuple[list[ToolOp], list[str]]:
    """Tool op cho catalog từ snapshot đã ghim. (ops, cảnh báo cho người)."""
    ops: list[ToolOp] = []
    warnings: list[str] = []
    if not cfg.enabled:
        return ops, warnings
    for srv in cfg.servers:
        snap = read_snapshot(state_dir, srv.name)
        if snap is None:
            warnings.append(f"mcp {srv.name}: chưa sync (ddt ext mcp sync {srv.name})")
            continue
        if not srv.pinned_sha:
            warnings.append(
                f"mcp {srv.name}: chưa pin schema (ddt ext mcp pin {srv.name}) → tool chưa dùng được"
            )
            continue
        if snap.get("sha256") != srv.pinned_sha:
            warnings.append(f"mcp {srv.name}: schema đã đổi so với pin → tool bị khoá, xem lại rồi pin lại")
            continue
        by_name = {str(t.get("name")): t for t in snap.get("tools") or [] if isinstance(t, dict)}
        for tool, risk in srv.tools.items():
            t = by_name.get(tool)
            if t is None:
                warnings.append(f"mcp {srv.name}: server không có tool {tool!r}")
                continue
            raw_schema = t.get("inputSchema")
            schema: dict[str, Any] = dict(raw_schema) if isinstance(raw_schema, dict) else {"type": "object"}
            ops.append(
                ToolOp(
                    name=op_name(srv.name, tool),
                    namespace="mcp",
                    op=op_name(srv.name, tool).split(".", 1)[1],
                    risk_tier=RiskTier(risk),
                    allowed_roles=list(srv.roles),
                    timeout_s=float(srv.timeout_sec),
                    description=f"[MCP {srv.name}] {str(t.get('description') or tool)[:400]}",
                    input_schema=schema,
                    sandbox_profile=None,
                    artifactize=True,
                    implemented=True,
                )
            )
    return ops, warnings


def role_grants(cfg: McpClientCfg, ops: Iterable[ToolOp]) -> dict[str, list[str]]:
    names = {op.name for op in ops}
    out: dict[str, list[str]] = {}
    for srv in cfg.servers:
        for tool in srv.tools:
            n = op_name(srv.name, tool)
            if n in names:
                for role in srv.roles:
                    out.setdefault(role, []).append(n)
    return out


# ───── kết nối ─────
class _Conn(Protocol):
    def request(self, method: str, params: Mapping[str, Any], timeout: float) -> dict[str, Any]: ...

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None: ...

    def close(self) -> None: ...


def _server_env(srv: McpServerCfg) -> dict[str, str]:
    env = {k: os.environ[k] for k in _SYS_ENV if k in os.environ}
    for k, v in srv.env.items():
        m = _ENV_REF.match(v)
        if m:
            if m.group(1) in os.environ:
                env[k] = os.environ[m.group(1)]
        else:
            env[k] = v
    return env


class _Stdio:
    def __init__(self, srv: McpServerCfg, *, cwd: Path, scratch: Path) -> None:
        self._p = PipeProcess(
            list(srv.command), cwd=cwd, env=_server_env(srv), stderr_path=scratch / f"mcp-{srv.name}.log"
        )
        self._id = 0

    def request(self, method: str, params: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        self._id += 1
        rid = self._id
        self._p.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params)}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise McpError(f"{method}: hết {timeout:g}s chờ server")
            line = self._p.recv(min(left, 1.0))
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # server in rác ra stdout — bỏ qua dòng đó
            if not isinstance(msg, dict) or msg.get("id") != rid:
                continue  # notification / request từ server: client tối giản không xử lý
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {}
                raise McpError(f"{method}: {str(err.get('message') or err)[:300]}")
            res = msg.get("result")
            return res if isinstance(res, dict) else {}

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._p.send(
            json.dumps({"jsonrpc": "2.0", "method": method, **({"params": dict(params)} if params else {})})
        )

    def close(self) -> None:
        self._p.close()


class _Http:
    def __init__(self, srv: McpServerCfg, network: NetworkCfg) -> None:
        v = check_url(str(srv.url), network)
        if not v.ok:
            raise McpError(f"url của {srv.name} bị chặn ({v.rule}): {v.reason}")
        self._url = str(srv.url)
        self._sid: str | None = None
        self._id = 0
        from digital_development_team.tools.web import tls_context

        self._ctx = tls_context()

    def _post(self, body: dict[str, Any], timeout: float) -> tuple[int, str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
            **({"Mcp-Session-Id": self._sid} if self._sid else {}),
        }
        req = urllib.request.Request(  # noqa: S310 - url đã qua netguard (https + allowlist)
            self._url, data=json.dumps(body).encode(ENCODING), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:  # noqa: S310
            self._sid = r.headers.get("Mcp-Session-Id") or self._sid
            return (
                r.status,
                str(r.headers.get("Content-Type") or ""),
                r.read(MAX_TEXT * 2).decode(ENCODING, errors="replace"),
            )

    def request(self, method: str, params: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        self._id += 1
        rid = self._id
        _, ctype, text = self._post(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params)}, timeout
        )
        msgs: list[Any] = []
        if "text/event-stream" in ctype:
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        msgs.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        continue
        else:
            try:
                msgs.append(json.loads(text))
            except json.JSONDecodeError as e:
                raise McpError(f"{method}: server trả không phải JSON") from e
        for msg in msgs:
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise McpError(f"{method}: {str(msg['error'])[:300]}")
                res = msg.get("result")
                return res if isinstance(res, dict) else {}
        raise McpError(f"{method}: không có phản hồi cho id {rid}")

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, **({"params": dict(params)} if params else {})}, 10.0)

    def close(self) -> None:
        return None


def connect(srv: McpServerCfg, *, cwd: Path, scratch: Path, network: NetworkCfg) -> _Conn:
    conn: _Conn = _Stdio(srv, cwd=cwd, scratch=scratch) if srv.transport == "stdio" else _Http(srv, network)
    try:
        conn.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "ddt", "version": __version__},
            },
            float(srv.timeout_sec),
        )
        conn.notify("notifications/initialized")
    except Exception:
        conn.close()
        raise
    return conn


def list_tools(conn: _Conn, timeout: float) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):
        res = conn.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
        tools += [t for t in res.get("tools") or [] if isinstance(t, dict)]
        cursor = res.get("nextCursor")
        if not cursor:
            break
    return tools


def sync(
    srv: McpServerCfg, *, cwd: Path, scratch: Path, network: NetworkCfg, state_dir: Path, now: str
) -> dict[str, Any]:
    """Khởi động server, lấy danh sách tool, ghi snapshot. Người xem rồi `pin` sha."""
    conn = connect(srv, cwd=cwd, scratch=scratch, network=network)
    try:
        tools = list_tools(conn, float(srv.timeout_sec))
    finally:
        conn.close()
    snap = {
        "server": srv.name,
        "synced_at": now,
        "sha256": schema_sha(tools),
        "tools": [
            {"name": t.get("name"), "description": t.get("description"), "inputSchema": t.get("inputSchema")}
            for t in tools
        ],
    }
    p = snapshot_path(state_dir, srv.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(p, json.dumps(snap, ensure_ascii=False, indent=2), fsync=False)
    return snap


class McpPool:
    """Kết nối lười tới các server đã ghim; handler cho từng tool lộ ra. Đóng hết khi process thoát."""

    def __init__(
        self,
        cfg: McpClientCfg,
        ops: Iterable[ToolOp],
        *,
        cwd: Path,
        scratch: Path,
        network: NetworkCfg,
        connector: Callable[[McpServerCfg], _Conn] | None = None,
    ) -> None:
        self._servers = {s.name: s for s in cfg.servers}
        self._ops = {op.name: op for op in ops}
        self._connect = connector or (lambda s: connect(s, cwd=cwd, scratch=scratch, network=network))
        self._conns: dict[str, _Conn] = {}
        self._locks: dict[str, threading.Lock] = {n: threading.Lock() for n in self._servers}
        self._calls: dict[tuple[str, str], int] = {}
        atexit.register(self.close)

    def handlers(self) -> dict[str, Handler]:
        out: dict[str, Handler] = {}
        for srv in self._servers.values():
            for tool in srv.tools:
                name = op_name(srv.name, tool)
                if name in self._ops:
                    out[name.split(".", 1)[1]] = self._handler(srv.name, tool)
        return out

    def _handler(self, server: str, tool: str) -> Handler:
        def run(args: Mapping[str, Any], ctx: ToolContext, profile: str | None) -> ToolResult:
            return self.call(server, tool, args, ctx)

        return run

    def _conn(self, srv: McpServerCfg) -> _Conn:
        conn = self._conns.get(srv.name)
        if conn is not None:
            return conn
        conn = self._connect(srv)
        live = schema_sha(list_tools(conn, float(srv.timeout_sec)))
        if live != srv.pinned_sha:
            conn.close()
            raise McpError("schema tool của server đã đổi so với bản đã ghim — người cần xem lại và pin lại")
        self._conns[srv.name] = conn
        return conn

    def call(self, server: str, tool: str, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        srv = self._servers[server]
        key = (ctx.issue_id, server)
        if self._calls.get(key, 0) >= srv.max_calls_per_issue:
            return ToolResult.denied(
                "mcp:cap",
                f"{server}: reached {srv.max_calls_per_issue} calls for this issue",
                hint="Use what you already have",
            )
        self._calls[key] = self._calls.get(key, 0) + 1
        with self._locks[server]:
            try:
                res = self._conn(srv).request(
                    "tools/call", {"name": tool, "arguments": dict(args)}, float(srv.timeout_sec)
                )
            except (McpError, EOFError, OSError) as e:
                bad = self._conns.pop(server, None)
                if bad is not None:
                    bad.close()
                return ToolResult.fail(
                    "MCP_ERROR", f"{server}.{tool}: {e}"[:300], hint="Retry once, or continue without it"
                )
        parts: list[str] = []
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
            elif isinstance(c, dict):
                parts.append(f"[{c.get('type')} content omitted]")
        text = "\n".join(parts)[:MAX_TEXT]
        extra: dict[str, Any] = {}
        sc = res.get("structuredContent")
        if isinstance(sc, dict) and len(json.dumps(sc)) < 4000:
            extra["data"] = sc
        if res.get("isError"):
            return ToolResult.fail("MCP_TOOL_ERROR", text[:300] or "tool error", hint="Check the arguments")
        return ToolResult.ok(f"{server}.{tool} → {len(text)} chars", content=text, **extra)

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception as e:  # pragma: no cover
                log.debug("đóng mcp lỗi: %s", e)
        self._conns.clear()


__all__ = [
    "McpError",
    "McpPool",
    "connect",
    "list_tools",
    "op_name",
    "read_snapshot",
    "role_grants",
    "schema_sha",
    "snapshot_ops",
    "snapshot_path",
    "sync",
]

"""Transport CLI — Codex (`codex exec`), nền tảng bắt buộc của cuộc thi. Đăng nhập `codex login`, không cần API key.

Hai mức (giống claude_cli_provider):
- chat()       Complete: 1 lần `codex exec --json`, shell tắt, sandbox read-only, không MCP.
- run_agent()  RunAgent: loop chạy TRONG process `codex`, mọi tool qua MCP server DDT (`ddt mcp --session`),
               cấu hình MCP bằng `-c mcp_servers.ddt.*` (không ghi ~/.codex/config.toml).

Cô lập: --ignore-user-config (không nạp MCP/plugin cá nhân) · --ignore-rules · project_doc_max_bytes=0 (KHÔNG nạp
AGENTS.md repo đích) · --disable shell_tool … · -s read-only (apply_patch built-in bị chặn → chỉ fs.write/edit DDT).
System prompt qua `-c model_instructions_file=<file>` (thay base instructions của Codex — gọn, ít token).

Luồng `--json` (0.146): thread.started · turn.started · item.started/updated/completed
(agent_message · reasoning · mcp_tool_call · command_execution · file_change · error) · turn.completed{usage} ·
turn.failed{error} · error. Usage chỉ có ở turn.completed (cuối lần exec) → mỗi mcp_tool_call báo 1 TurnInfo usage 0
(trục turns/wall kill được giữa chừng), usage cộng một lần ở TurnInfo cuối.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from digital_development_team.config.models import CodexCfg, ProviderCfg
from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import ProviderError, ProviderUnavailable, RateLimited
from digital_development_team.core.llm import (
    ChatRequest,
    ChatResponse,
    Message,
    ProviderCaps,
    StopReason,
    TextBlock,
    Usage,
)
from digital_development_team.llm.claude_cli_provider import ClaudeCliProvider, split_binary
from digital_development_team.llm.cli_session import CliAgentResult, CliSession, TurnInfo
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.proc import stream_process

log = logging.getLogger(__name__)

PROVIDER = "codex_cli"
MCP_SERVER = "ddt"
_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "hit your limit",
    "limit reached",
    "too many requests",
    "insufficient_quota",
    "quota exceeded",
    "overloaded",
    "server is busy",
)
_LOGIN_PATTERNS = ("not logged in", "codex login", "401 unauthorized", "unauthorized", "please log in")
_MODEL_PATTERNS = ("model_not_found", "model not found", "does not exist", "not supported when using codex")
# Biến môi trường chuyển cho MCP server DDT (Codex KHÔNG truyền toàn bộ env cho MCP stdio)
_MCP_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


# ───── TOML cho -c key=value ─────
def toml_value(v: Any) -> str:
    """Giá trị Python → literal TOML một dòng (chuỗi dùng basic string có escape, path Windows an toàn)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string ⊂ TOML basic string
    if isinstance(v, list | tuple):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, Mapping):
        return "{" + ", ".join(f"{_toml_key(k)} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"không đổi được sang TOML: {type(v).__name__}")


def _toml_key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k)


# ───── parse --json ─────
def usage_from(u: Mapping[str, Any] | None, latency_ms: int = 0) -> Usage:
    """OpenAI: input_tokens ĐÃ gồm cached_input_tokens (khác Anthropic) — khớp quy ước Usage.tokens_in."""
    if not u:
        return Usage(latency_ms=latency_ms)
    return Usage(
        tokens_in=int(u.get("input_tokens") or 0),
        tokens_out=int(u.get("output_tokens") or 0),
        tokens_cached=int(u.get("cached_input_tokens") or 0),
        latency_ms=latency_ms,
    )


_HTTP_429 = re.compile(r"\b429\b")  # không khớp nhầm hex trong cf-ray


def looks_rate_limited(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _LIMIT_PATTERNS) or bool(_HTTP_429.search(low))


def is_unrecognized_model(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _MODEL_PATTERNS)


class CodexStreamParser:
    """Gom JSONL của `codex exec --json` thành TurnInfo. Mỗi mcp_tool_call hoàn tất = 1 lượt (usage 0, trục turns);
    turn.completed = lượt cuối mang usage của cả lần exec. on_turn trả False → harness muốn dừng → kill process."""

    def __init__(self, result: CliAgentResult, on_turn: Callable[[TurnInfo], bool], model: str = "") -> None:
        self._result = result
        self._on_turn = on_turn
        self._model = model
        self._texts: list[str] = []
        self.turns = 0
        self.stop_requested = False
        self.thread_id: str | None = None
        self.builtin_calls: list[str] = []  # tool built-in lọt qua (lẽ ra đã tắt) — để log/kiểm

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return
        t = str(d.get("type") or "")
        if t == "thread.started":
            self.thread_id = d.get("thread_id")
        elif t == "item.completed":
            self._item(d.get("item") or {})
        elif t == "turn.completed":
            self._turn_done(d.get("usage"))
        elif t == "turn.failed":
            self._failed(str((d.get("error") or {}).get("message") or "turn.failed"))
        elif t == "error":
            self._failed(str(d.get("message") or "error"), fatal=False)

    def _item(self, item: Mapping[str, Any]) -> None:
        kind = str(item.get("type") or "")
        if kind == "agent_message":
            text = str(item.get("text") or "")
            if text:
                self._texts.append(text)
                self._result.final_text = text
        elif kind == "mcp_tool_call":
            server, tool = str(item.get("server") or ""), str(item.get("tool") or "")
            name = tool if server in ("", MCP_SERVER) else f"{server}.{tool}"
            self._emit((name,), Usage(), "tool_use")
        elif kind in ("command_execution", "file_change", "web_search", "collab_tool_call"):
            self.builtin_calls.append(kind)
            log.warning("codex_cli: tool built-in %s chạy dù đã tắt — kiểm codex.disable_features", kind)
            self._emit((f"codex.{kind}",), Usage(), "tool_use")
        elif kind == "error":
            msg = str(item.get("message") or "")
            if looks_rate_limited(msg):
                self._result.rate_limited = True

    def _turn_done(self, usage: Mapping[str, Any] | None) -> None:
        u = usage_from(usage)
        r = self._result
        r.usage = Usage(
            tokens_in=r.usage.tokens_in + u.tokens_in,
            tokens_out=r.usage.tokens_out + u.tokens_out,
            tokens_cached=r.usage.tokens_cached + u.tokens_cached,
            latency_ms=r.usage.latency_ms,
        )
        self._emit((), u, "end_turn")

    def _failed(self, message: str, *, fatal: bool = True) -> None:
        r = self._result
        if fatal:
            r.is_error = True
            r.subtype = "turn_failed"
        if looks_rate_limited(message):
            r.rate_limited = True
        if fatal or not r.final_text:
            r.stderr_tail = (r.stderr_tail + "\n" + message).strip()[-800:]

    def _emit(self, calls: tuple[str, ...], usage: Usage, stop: str) -> None:
        self.turns += 1
        self._result.turns = self.turns
        info = TurnInfo(self.turns, "\n".join(self._texts), calls, usage, stop, self._model)
        self._texts = []
        if not self.stop_requested and not self._on_turn(info):
            self.stop_requested = True


def resolve_binary(head: str) -> str:
    """`codex` → đường dẫn thật. Windows: npm cài wrapper codex.cmd (node) → gọi THẲNG codex.exe native trong
    vendor/ (tránh cmd.exe băm đối số TOML có ngoặc/nháy và trần 8K ký tự dòng lệnh)."""
    found = head if Path(head).exists() else (shutil.which(head) or head)
    p = Path(found)
    if p.suffix.lower() in (".cmd", ".bat", ".ps1") or (os.name == "nt" and p.suffix == ""):
        root = p.parent / "node_modules" / "@openai" / "codex"
        for pat in ("node_modules/@openai/codex-*/vendor/*/bin/codex.exe", "vendor/*/codex/codex.exe"):
            hits = sorted(root.glob(pat)) if root.is_dir() else []
            if hits:
                return str(hits[0])
    return found


# ───── provider ─────
class CodexCliProvider:
    name = PROVIDER
    caps = ProviderCaps(
        tool_use=True, prompt_cache=True, streaming=True, append_only_history=True, agent_loop=True
    )

    def __init__(
        self, cfg: ProviderCfg, *, scratch: Path, python_exe: str | None = None, timeout_s: float = 1800.0
    ) -> None:
        self._cfg = cfg
        self._codex: CodexCfg = cfg.codex or CodexCfg()
        self._scratch = scratch
        self._python = python_exe or sys.executable
        self._timeout = timeout_s
        parts = split_binary(cfg.binary or "codex")
        self._argv0: list[str] = [resolve_binary(parts[0]), *parts[1:]]

    @staticmethod
    def binary_available(cfg: ProviderCfg) -> bool:
        return ClaudeCliProvider.binary_available(cfg) if cfg.binary else shutil.which("codex") is not None

    # ───── argv ─────
    def base_argv(self, *, model: str, system_file: Path, cwd: Path, effort: str | None = None) -> list[str]:
        c = self._codex
        argv = [
            *self._argv0,
            "exec",
            "--json",
            "--ephemeral",  # không lưu session (giống --no-session-persistence)
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            str(cwd),
            "-m",
            model,
            "-s",
            c.sandbox,
        ]
        if c.ignore_user_config:
            argv.append("--ignore-user-config")
        if c.ignore_rules:
            argv.append("--ignore-rules")
        for feat in c.disable_features:
            argv += ["--disable", feat]
        overrides: dict[str, Any] = {
            "approval_policy": "never",
            "project_doc_max_bytes": c.project_doc_max_bytes,
            "model_instructions_file": str(system_file),
            "check_for_update_on_startup": False,
            "web_search": "disabled",
            **c.extra_config,
        }
        if effort:
            overrides["model_reasoning_effort"] = effort
        for k, v in overrides.items():
            argv += ["-c", f"{k}={toml_value(v)}"]
        return argv

    def agent_argv(
        self, *, model: str, system_file: Path, cwd: Path, session_path: Path, effort: str | None
    ) -> list[str]:
        argv = self.base_argv(model=model, system_file=system_file, cwd=cwd, effort=effort)
        c = self._codex
        mcp: dict[str, Any] = {
            "command": self._python,
            "args": ["-m", "digital_development_team.cli.mcp_serve", "--session", str(session_path)],
            "env": self._mcp_env(),
            "startup_timeout_sec": c.mcp_startup_timeout_s,
            "tool_timeout_sec": c.mcp_tool_timeout_s,
        }
        for k, v in mcp.items():
            argv += ["-c", f"mcp_servers.{MCP_SERVER}.{k}={toml_value(v)}"]
        argv.append("-")  # prompt từ stdin
        return argv

    def _mcp_env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in _MCP_ENV_KEYS if os.environ.get(k)}
        env["PYTHONUTF8"] = "1"
        return env

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("DDT_")}
        env["PYTHONUTF8"] = "1"
        env["NO_COLOR"] = "1"
        if self._codex.codex_home:
            env["CODEX_HOME"] = self._codex.codex_home
        return env

    # ───── Complete ─────
    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        cancel.raise_if_cancelled()
        if req.tools:
            log.info("codex_cli Complete: bỏ %d tool (chế độ 1 call không có tool)", len(req.tools))
        sys_file = self._write_system(req.system)
        argv = [
            *self.base_argv(
                model=req.model.model, system_file=sys_file, cwd=self._scratch, effort=req.model.effort
            ),
            "-",
        ]
        result = CliAgentResult(model=req.model.model)
        parser = CodexStreamParser(result, lambda _i: True, req.model.model)
        t0 = time.monotonic()
        try:
            res, stderr = self._run(
                argv, cancel=cancel, on_line=parser.feed, stdin_text=ClaudeCliProvider.render_prompt(req)
            )
        finally:
            with contextlib.suppress(OSError):
                sys_file.unlink()
        latency = int((time.monotonic() - t0) * 1000)
        if res.exit_code != 0 or result.is_error or not result.final_text:
            raise self._map_failure(
                res.exit_code, f"{stderr}\n{result.stderr_tail}", rate=result.rate_limited
            )
        return ChatResponse(
            message=Message(role="assistant", blocks=(TextBlock(text=result.final_text),)),
            stop=StopReason.END_TURN,
            usage=Usage(
                tokens_in=result.usage.tokens_in,
                tokens_out=result.usage.tokens_out,
                tokens_cached=result.usage.tokens_cached,
                latency_ms=latency,
            ),
            model=req.model.model,
            raw_id=parser.thread_id,
        )

    # ───── RunAgent ─────
    def run_agent(
        self,
        *,
        session: CliSession,
        system: str,
        brief: str,
        model: str,
        effort: str | None,
        cancel: CancelToken,
        on_turn: Callable[[TurnInfo], bool],
        timeout_s: float | None = None,
    ) -> CliAgentResult:
        cancel.raise_if_cancelled()
        self._scratch.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex[:8]
        if session.permission_tool:  # Codex không có permission prompt tool (approval_policy=never)
            from dataclasses import replace

            session = replace(session, permission_tool=False)
        session_path = session.dump(
            self._scratch / f"cli-session-{session.issue_id}-{session.role}-{token}.json"
        )
        sys_file = self._write_system(system, token)
        cwd = Path(session.worktree) if session.worktree else Path(session.repo or self._scratch)
        argv = self.agent_argv(
            model=model, system_file=sys_file, cwd=cwd, session_path=session_path, effort=effort
        )
        result = CliAgentResult(model=model)
        parser = CodexStreamParser(result, on_turn, model)
        try:
            res, stderr = self._run(
                argv,
                cancel=cancel,
                on_line=parser.feed,
                stop=lambda: parser.stop_requested,
                cwd=cwd,
                timeout_s=timeout_s,
                stdin_text=brief,
            )
        finally:
            with contextlib.suppress(OSError):
                sys_file.unlink()
        result.exit_code = res.exit_code
        result.killed = parser.stop_requested or res.timed_out or res.cancelled
        result.stderr_tail = (result.stderr_tail + "\n" + stderr[-800:]).strip()[-800:]
        if not result.killed and res.exit_code != 0 and looks_rate_limited(result.stderr_tail):
            result.rate_limited = True
        if not result.killed and res.exit_code != 0 and not result.rate_limited and result.turns == 0:
            raise self._map_failure(res.exit_code, result.stderr_tail)
        return result

    # ───── nội bộ ─────
    def _write_system(self, system: str, token: str | None = None) -> Path:
        self._scratch.mkdir(parents=True, exist_ok=True)
        path = self._scratch / f"codex-system-{token or uuid.uuid4().hex[:8]}.md"
        write_text_atomic(path, system, fsync=False)
        return path

    def _run(
        self,
        argv: list[str],
        *,
        cancel: CancelToken,
        on_line: Callable[[str], None],
        stop: Callable[[], bool] | None = None,
        cwd: Path | None = None,
        timeout_s: float | None = None,
        stdin_text: str | None = None,
    ) -> tuple[Any, str]:
        self._scratch.mkdir(parents=True, exist_ok=True)
        err_path = self._scratch / f"codex-{uuid.uuid4().hex[:8]}.err"
        try:
            res = stream_process(
                argv,
                cwd=cwd or self._scratch,
                env=self._env(),
                timeout_s=timeout_s or self._timeout,
                stderr_path=err_path,
                cancel=cancel,
                on_line=on_line,
                stop=stop,
                stdin_text=stdin_text,
            )
            stderr = err_path.read_text(encoding=ENCODING, errors="replace") if err_path.exists() else ""
        finally:
            with contextlib.suppress(OSError):
                err_path.unlink(missing_ok=True)
        return res, stderr

    @staticmethod
    def _map_failure(exit_code: int, text: str, *, rate: bool = False) -> ProviderError:
        snippet = (text or "").strip()[-300:]
        low = snippet.lower()
        if rate or looks_rate_limited(snippet):
            return RateLimited(f"codex_cli hết hạn mức/429: {snippet}")
        if any(p in low for p in _LOGIN_PATTERNS):
            return ProviderError(
                f"codex_cli chưa đăng nhập: {snippet}", hint="chạy `codex login` (kiểm: `codex login status`)"
            )
        if is_unrecognized_model(snippet):
            return ProviderError(
                f"codex_cli: tài khoản không có model: {snippet}",
                hint="đổi aliases trong preset codex_all (models.yaml) sang model tài khoản Codex có",
            )
        if exit_code != 0 and not snippet:
            return ProviderUnavailable(f"codex_cli exit {exit_code} không có output")
        return ProviderUnavailable(f"codex_cli exit {exit_code}: {snippet}")

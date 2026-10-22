"""Transport CLI — Claude Code headless (`claude -p`), đăng nhập subscription, không cần API key.

Hai mức:
- chat()       Complete: 1 call, `--output-format json`, không tool (role một call khi mọi API tắt).
- run_agent()  RunAgent: loop chạy TRONG process `claude`, mọi tool qua MCP server DDT (`ddt mcp --session`),
               `--output-format stream-json` → usage từng lượt về harness (budget 3 trục, kill khi chạm trần).

Cờ bắt buộc (models.yaml#providers.anthropic_cli.cli): --tools "" (tắt built-in) · --mcp-config + --strict-mcp-config ·
--setting-sources user (KHÔNG nạp CLAUDE.md/.claude của repo đích) · --permission-prompt-tool mcp__ddt__policy ·
--no-session-persistence. CLI 2.1 không có --max-turns → harness đếm và kill.
Hết hạn mức (rate_limit_event.status != allowed · result api_error_status 429 · stderr pattern) → RateLimited (như 429).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from digital_development_team.config.models import ProviderCfg
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
from digital_development_team.llm.cli_session import CliAgentResult, CliSession, TurnInfo
from digital_development_team.util.fsio import ENCODING, write_text_atomic
from digital_development_team.util.proc import stream_process

log = logging.getLogger(__name__)

__all__ = ["ClaudeCliProvider", "CliAgentResult", "CliSession", "StreamParser", "TurnInfo"]

PROVIDER = "anthropic_cli"
PERMISSION_TOOL = "mcp__ddt__policy"
MCP_SERVER = "ddt"
_LIMIT_PATTERNS = (
    "rate limit",
    "usage limit",
    "hit your limit",
    "limit reached",
    "too many requests",
    "429",
    "overloaded",
)
_STOP = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}


# ───── parse stream-json ─────
def _usage_from(u: Mapping[str, Any] | None, latency_ms: int = 0) -> Usage:
    if not u:
        return Usage(latency_ms=latency_ms)
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    return Usage(
        tokens_in=int(u.get("input_tokens") or 0) + cache_read + cache_write,
        tokens_out=int(u.get("output_tokens") or 0),
        tokens_cached=cache_read,
        latency_ms=latency_ms,
    )


def parse_assistant_event(d: Mapping[str, Any], index: int) -> TurnInfo:
    msg = d.get("message") or {}
    texts: list[str] = []
    calls: list[str] = []
    for b in msg.get("content") or []:
        if b.get("type") == "text":
            texts.append(str(b.get("text", "")))
        elif b.get("type") == "tool_use":
            calls.append(str(b.get("name", "")))
    return TurnInfo(
        index,
        "".join(texts),
        tuple(calls),
        _usage_from(msg.get("usage")),
        msg.get("stop_reason"),
        str(msg.get("model", "")),
    )


class StreamParser:
    """Gom dòng stream-json thành lượt. CLI phát 1 dòng `assistant` cho MỖI content block, cùng `message.id`
    và usage lặp lại → gộp theo id; lượt được chốt khi id đổi hoặc gặp `result` (budget trễ đúng 1 lượt).
    """

    def __init__(self, result: CliAgentResult, on_turn: Callable[[TurnInfo], bool]) -> None:
        self._result = result
        self._on_turn = on_turn
        self._pending_id: str | None = None
        self._pending: dict[str, Any] | None = None
        self._texts: list[str] = []
        self._calls: list[str] = []
        self.turns = 0
        self.stop_requested = False

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return
        t = d.get("type")
        if t == "assistant":
            self._assistant(d)
        elif t == "rate_limit_event" and rate_limit_hit(d):
            self._result.rate_limited = True
        elif t == "result":
            self.flush()
            self._final(d)

    def _assistant(self, d: Mapping[str, Any]) -> None:
        msg = d.get("message") or {}
        mid = str(msg.get("id") or f"noid-{self.turns + 1}-{len(self._texts)}")
        if self._pending is not None and mid != self._pending_id:
            self.flush()
        if self._pending is None:
            self._pending_id, self._texts, self._calls = mid, [], []
        self._pending = dict(msg)  # usage/stop_reason: lấy bản mới nhất
        for b in msg.get("content") or []:
            if b.get("type") == "text":
                self._texts.append(str(b.get("text", "")))
            elif b.get("type") == "tool_use":
                self._calls.append(str(b.get("name", "")))

    def flush(self) -> None:
        if self._pending is None:
            return
        self.turns += 1
        msg, self._pending = self._pending, None
        info = TurnInfo(
            self.turns,
            "".join(self._texts),
            tuple(self._calls),
            _usage_from(msg.get("usage")),
            msg.get("stop_reason"),
            str(msg.get("model", "")),
        )
        r = self._result
        r.turns = self.turns
        r.usage = Usage(
            tokens_in=r.usage.tokens_in + info.usage.tokens_in,
            tokens_out=r.usage.tokens_out + info.usage.tokens_out,
            tokens_cached=r.usage.tokens_cached + info.usage.tokens_cached,
            latency_ms=r.usage.latency_ms,
        )
        if info.text:
            r.final_text = info.text
        if not self._on_turn(info):
            self.stop_requested = True

    def _final(self, d: Mapping[str, Any]) -> None:
        r = self._result
        r.subtype = d.get("subtype")
        r.is_error = bool(d.get("is_error"))
        r.permission_denials = list(d.get("permission_denials") or [])
        r.turns = int(d.get("num_turns") or r.turns)
        if d.get("result"):
            r.final_text = str(d["result"])
        if d.get("usage"):
            r.usage = _usage_from(d.get("usage"), r.usage.latency_ms)
        if d.get("api_error_status") == 429 or (r.is_error and looks_rate_limited(r.final_text)):
            r.rate_limited = True


def rate_limit_hit(d: Mapping[str, Any]) -> bool:
    info = d.get("rate_limit_info") or {}
    return str(info.get("status", "allowed")).lower() not in ("allowed", "ok", "")


def looks_rate_limited(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _LIMIT_PATTERNS)


def is_mcp_ddt_tool(name: str) -> bool:
    return name.startswith(f"mcp__{MCP_SERVER}__")


def mcp_tool_to_wire(name: str) -> str:
    return name.removeprefix(f"mcp__{MCP_SERVER}__")


def resolve_binary(head: str) -> str:
    """`claude` → đường dẫn thật. Windows: npm cài wrapper claude.cmd → gọi THẲNG bin/claude.exe cạnh nó
    (tránh cmd.exe băm đối số có xuống dòng/ký tự đặc biệt và trần 8K ký tự dòng lệnh)."""
    found = head if Path(head).exists() else (shutil.which(head) or head)
    p = Path(found)
    if p.suffix.lower() in (".cmd", ".bat"):
        native = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.exists():
            return str(native)
    return found


def split_binary(raw: str | None) -> list[str]:
    """models.yaml#binary: 'claude' | '"C:/x y/claude.cmd" --flag' → argv đầu. Windows: giữ backslash, bỏ ngoặc kép bao."""
    raw = (raw or "claude").strip()
    if " " not in raw:
        return [raw.strip('"')]
    return [t.strip('"') for t in shlex.split(raw, posix=os.name != "nt")]


_FAMILY_ALIASES = (("claude-opus-", "opus"), ("claude-sonnet-", "sonnet"), ("claude-haiku-", "haiku"))


def _results_from(text: str, limit: int) -> list[dict[str, str]]:
    """JSON {"results": [...]} trong câu trả lời (có thể nằm trong code fence) → list đã chuẩn hoá."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        d = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    out: list[dict[str, str]] = []
    for r in d.get("results") or [] if isinstance(d, dict) else []:
        if isinstance(r, dict) and str(r.get("url") or "").startswith("http"):
            out.append(
                {
                    "title": str(r.get("title") or "")[:200],
                    "url": str(r["url"])[:500],
                    "snippet": str(r.get("snippet") or "")[:200],
                }
            )
    return out[:limit]


def family_alias(model: str) -> str | None:
    """`claude-sonnet-5-5` → `sonnet` (alias CLI tự phân giải sang bản mới nhất của họ đó). Không biết → None."""
    for prefix, alias in _FAMILY_ALIASES:
        if model.startswith(prefix):
            return alias
    return None


def is_unrecognized_model(text: str) -> bool:
    return "unrecognized_model" in text or "model not found" in text.lower()


# ───── provider ─────
class ClaudeCliProvider:
    name = PROVIDER
    caps = ProviderCaps(
        tool_use=True, prompt_cache=True, streaming=True, append_only_history=True, agent_loop=True
    )

    def __init__(
        self, cfg: ProviderCfg, *, scratch: Path, python_exe: str | None = None, timeout_s: float = 1800.0
    ) -> None:
        self._cfg = cfg
        self._cli = cfg.cli
        self._scratch = scratch
        self._python = python_exe or sys.executable
        self._timeout = timeout_s
        parts = split_binary(cfg.binary)
        self._argv0: list[str] = [resolve_binary(parts[0]), *parts[1:]]
        self._model_override: dict[
            str, str
        ] = {}  # ID pin mà CLI không nhận → alias họ model (nhớ trong process)

    @staticmethod
    def binary_available(cfg: ProviderCfg) -> bool:
        head = split_binary(cfg.binary)[0]
        return Path(head).exists() or shutil.which(head) is not None

    # ───── argv ─────
    def base_argv(
        self, *, model: str, system_file: Path, output_format: str, effort: str | None = None
    ) -> list[str]:
        sources = ",".join(self._cli.setting_sources) if self._cli else "user"
        argv = [
            *self._argv0,
            "-p",
            "--output-format",
            output_format,
            "--tools",
            "",  # tắt TOÀN BỘ built-in tool — chỉ MCP DDT
            "--setting-sources",
            sources,  # không nạp CLAUDE.md/.claude của repo đích
            "--strict-mcp-config",
            "--no-session-persistence",
            "--model",
            model,
            "--system-prompt-file",  # file: không đi qua dòng lệnh (dài, nhiều dòng)
            str(system_file),
        ]
        if output_format == "stream-json":
            argv.append("--verbose")
        if effort:
            argv += ["--effort", effort]
        return argv

    def agent_argv(
        self, *, model: str, system_file: Path, mcp_config: Path, effort: str | None = None
    ) -> list[str]:
        argv = self.base_argv(
            model=model, system_file=system_file, output_format="stream-json", effort=effort
        )
        argv += ["--mcp-config", str(mcp_config), "--allowedTools", f"mcp__{MCP_SERVER}"]
        tool = (self._cli.permission_prompt_tool if self._cli else None) or PERMISSION_TOOL
        argv += ["--permission-prompt-tool", tool]
        return argv

    def write_mcp_config(self, path: Path, session_path: Path) -> Path:
        cfg = {
            "mcpServers": {
                MCP_SERVER: {
                    "command": self._python,
                    "args": ["-m", "digital_development_team.cli.mcp_serve", "--session", str(session_path)],
                }
            }
        }
        write_text_atomic(path, json.dumps(cfg, ensure_ascii=False, indent=1), fsync=False)
        return path

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("DDT_")}
        env["PYTHONUTF8"] = "1"
        env["NO_COLOR"] = "1"
        return env

    # ───── Complete ─────
    @staticmethod
    def render_prompt(req: ChatRequest) -> str:
        """Hội thoại → 1 prompt text (CLI -p nhận 1 chuỗi). Lượt đầu giữ nguyên brief."""
        parts: list[str] = []
        for i, m in enumerate(req.messages):
            text = m.text.strip()
            if not text:
                continue
            if i == 0 and m.role == "user":
                parts.append(text)
            else:
                parts.append(("ASSISTANT (trước đó):\n" if m.role == "assistant" else "USER:\n") + text)
        return "\n\n".join(parts)

    def cli_model(self, model: str) -> str:
        return self._model_override.get(model, model)

    def _fallback_model(self, model: str, text: str) -> str | None:
        """CLI không nhận ID → alias họ model (1 lần). Trả None nếu không áp dụng."""
        alias = family_alias(model)
        if not is_unrecognized_model(text) or alias is None or self._model_override.get(model) == alias:
            return None
        log.warning("claude_cli: CLI không nhận model %s → dùng alias %r", model, alias)
        self._model_override[model] = alias
        return alias

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        try:
            return self._chat_once(req, cancel=cancel)
        except ProviderError as e:
            if self._fallback_model(req.model.model, e.message) is None:
                raise
            return self._chat_once(req, cancel=cancel)

    def _chat_once(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        cancel.raise_if_cancelled()
        if req.tools:
            log.info("claude_cli Complete: bỏ %d tool (chế độ 1 call không có tool)", len(req.tools))
        sys_file = self._write_system(req.system)
        argv = self.base_argv(
            model=self.cli_model(req.model.model),
            system_file=sys_file,
            output_format="json",
            effort=req.model.effort,
        )
        lines: list[str] = []
        t0 = time.monotonic()
        try:
            res, stderr = self._run(
                argv, cancel=cancel, on_line=lines.append, stdin_text=self.render_prompt(req)
            )
        finally:
            with contextlib.suppress(OSError):
                sys_file.unlink()
        latency = int((time.monotonic() - t0) * 1000)
        raw = "\n".join(lines).strip()
        if res.exit_code != 0 or not raw:
            raise self._map_failure(res.exit_code, stderr or raw)
        try:
            d = json.loads(raw[raw.index("{") :]) if "{" in raw else {}
        except json.JSONDecodeError as e:
            raise ProviderError(f"claude_cli: output không phải JSON: {raw[:200]}") from e
        if d.get("is_error"):
            raise self._map_failure(
                1, str(d.get("result") or d.get("subtype")), api_status=d.get("api_error_status")
            )
        text = str(d.get("result") or "")
        model = next(iter((d.get("modelUsage") or {}).keys()), req.model.model)
        return ChatResponse(
            message=Message(role="assistant", blocks=(TextBlock(text=text),)),
            stop=_STOP.get(str(d.get("stop_reason")), StopReason.END_TURN),
            usage=_usage_from(d.get("usage"), latency),
            model=str(model),
            raw_id=d.get("session_id"),
        )

    # ───── WebSearch (tool web.search của DDT) ─────
    def web_search(
        self,
        query: str,
        *,
        model: str,
        max_results: int,
        prefer_hosts: Sequence[str],
        cancel: CancelToken,
        timeout_s: float = 120.0,
    ) -> list[dict[str, str]]:
        """Một call `claude -p` CHỈ bật WebSearch (built-in của CLI, subscription, không cần key). Không MCP, không
        tool khác, không đọc repo. Trả [{title, url, snippet}]; harness lọc/đánh dấu theo allowlist sau."""
        cancel.raise_if_cancelled()
        prefer = f" Prefer official documentation on: {', '.join(prefer_hosts[:20])}." if prefer_hosts else ""
        system = (
            "You are a web search helper. Use the WebSearch tool, then reply with ONLY one JSON object "
            '{"results":[{"title":"","url":"","snippet":""}]} with at most '
            f"{max_results} results and snippets under 200 characters. No prose.{prefer}"
        )
        sys_file = self._write_system(system)
        argv = self.base_argv(model=self.cli_model(model), system_file=sys_file, output_format="json")
        argv[argv.index("--tools") + 1] = "WebSearch"
        argv += ["--allowedTools", "WebSearch"]
        lines: list[str] = []
        try:
            res, stderr = self._run(
                argv,
                cancel=cancel,
                on_line=lines.append,
                timeout_s=timeout_s,
                stdin_text=f"Search the web for: {query}",
            )
        finally:
            with contextlib.suppress(OSError):
                sys_file.unlink()
        raw = "\n".join(lines).strip()
        if res.exit_code != 0 or not raw:
            raise self._map_failure(res.exit_code, stderr or raw)
        try:
            d = json.loads(raw[raw.index("{") :]) if "{" in raw else {}
        except json.JSONDecodeError as e:
            raise ProviderError(f"claude_cli: output không phải JSON: {raw[:200]}") from e
        if d.get("is_error"):
            raise self._map_failure(
                1, str(d.get("result") or d.get("subtype")), api_status=d.get("api_error_status")
            )
        return _results_from(str(d.get("result") or ""), max_results)

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
        """on_turn trả False → harness muốn dừng (budget) → kill process. Tool call đi qua MCP DDT.
        CLI không nhận ID model ở lượt 0 → thử lại 1 lần với alias họ model."""
        kw = {
            "session": session,
            "system": system,
            "brief": brief,
            "model": model,
            "effort": effort,
            "cancel": cancel,
            "on_turn": on_turn,
            "timeout_s": timeout_s,
        }
        try:
            res = self._run_agent_once(**kw)  # type: ignore[arg-type]
        except ProviderError as e:
            if self._fallback_model(model, e.message) is None:
                raise
            return self._run_agent_once(**kw)  # type: ignore[arg-type]
        if res.turns == 0 and self._fallback_model(model, f"{res.stderr_tail} {res.final_text}") is not None:
            return self._run_agent_once(**kw)  # type: ignore[arg-type]
        return res

    def _run_agent_once(
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
        session_path = session.dump(
            self._scratch / f"cli-session-{session.issue_id}-{session.role}-{token}.json"
        )
        mcp_path = self.write_mcp_config(
            self._scratch / f"ddt-mcp-{session.issue_id}-{token}.json", session_path
        )
        sys_file = self._write_system(system, token)
        argv = self.agent_argv(
            model=self.cli_model(model), system_file=sys_file, mcp_config=mcp_path, effort=effort
        )
        result = CliAgentResult(model=model)
        parser = StreamParser(result, on_turn)

        cwd = Path(session.worktree) if session.worktree else Path(session.repo or self._scratch)
        try:
            res, stderr = self._run(
                argv,
                cancel=cancel,
                on_line=parser.feed,
                stop=lambda: parser.stop_requested,
                cwd=cwd,
                timeout_s=timeout_s,
                stdin_text=brief,  # brief qua stdin: không giới hạn độ dài, không băm ký tự
            )
        finally:
            with contextlib.suppress(OSError):
                sys_file.unlink()
        result.exit_code = res.exit_code
        result.killed = parser.stop_requested or res.timed_out or res.cancelled
        result.stderr_tail = stderr[-800:]
        if not result.killed and res.exit_code != 0 and looks_rate_limited(stderr):
            result.rate_limited = True
        if not result.killed and res.exit_code != 0 and not result.rate_limited and result.turns == 0:
            raise self._map_failure(res.exit_code, stderr)
        return result

    # ───── nội bộ ─────
    def _write_system(self, system: str, token: str | None = None) -> Path:
        self._scratch.mkdir(parents=True, exist_ok=True)
        path = self._scratch / f"claude-system-{token or uuid.uuid4().hex[:8]}.md"
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
        err_path = self._scratch / f"claude-{uuid.uuid4().hex[:8]}.err"
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
    def _map_failure(exit_code: int, text: str, *, api_status: Any = None) -> ProviderError:
        snippet = (text or "").strip()[-300:]
        if api_status == 429 or looks_rate_limited(snippet):
            return RateLimited(f"claude_cli hết hạn mức/429: {snippet}")
        if "not logged in" in snippet.lower() or ("login" in snippet.lower() and "claude" in snippet.lower()):
            return ProviderError(
                f"claude_cli chưa đăng nhập: {snippet}",
                hint="chạy `claude` một lần để đăng nhập subscription",
            )
        if is_unrecognized_model(snippet):
            return ProviderError(
                f"claude_cli: CLI không nhận model: {snippet}",
                hint="đổi aliases.*_cli trong models.yaml sang ID CLI hiện hỗ trợ (vd `claude -p --model sonnet`)",
            )
        if exit_code != 0 and not snippet:
            return ProviderUnavailable(f"claude_cli exit {exit_code} không có output")
        return ProviderUnavailable(f"claude_cli exit {exit_code}: {snippet}")

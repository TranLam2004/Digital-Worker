"""HttpGameMaster — client stdlib (urllib) cho Game Master thật.

- Chỉ gọi đúng 4 đường dẫn khai trong policy.yaml#gamemaster.paths (luật: cấm Admin/Grader API, cấm dò đề chưa mở).
- GET trang match luôn lấy clock mới; 429/5xx → chờ theo Retry-After rồi thử lại.
- Submit gửi Idempotency-Key; retry dùng CÙNG key và CÙNG body → server trả lại bài cũ, không thành nộp 2 lần.
- Token lấy từ biến môi trường (gamemaster.token_env), không ghi ra log/event.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

from digital_development_team.config.models import GameMasterCfg
from digital_development_team.core.errors import ConfigError, DDTError
from digital_development_team.gamemaster.models import (
    GradeResult,
    MatchSnapshot,
    StartResult,
    SubmitResult,
    parse_grade,
    parse_snapshot,
    parse_start,
    parse_submit,
)

log = logging.getLogger(__name__)
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class GameMasterError(DDTError):
    code = "GAMEMASTER"

    def __init__(self, message: str, *, status: int | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.status = status


class HttpGameMaster:
    def __init__(
        self,
        cfg: GameMasterCfg,
        match_id: str,
        *,
        env: Mapping[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not cfg.base_url:
            raise ConfigError(
                "gamemaster.base_url chưa cấu hình",
                hint="ddt runtime set policy.gamemaster.base_url=https://…",
            )
        if not str(cfg.base_url).startswith(("https://", "http://")):
            raise ConfigError(f"gamemaster.base_url phải là http(s): {cfg.base_url!r}")
        host = (urlsplit(str(cfg.base_url)).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1") and not cfg.http_contract_confirmed:
            raise ConfigError(
                "Direct HTTP Game Master contract is not confirmed",
                hint="Use the organizer's Game Player adapter, or explicitly confirm the authorized HTTP contract",
            )
        env = env if env is not None else os.environ
        self._cfg = cfg
        self._match_id = match_id
        self._token = env.get(cfg.token_env, "")
        self._team = env.get(cfg.team_env, "")
        self._now = now or (lambda: datetime.now().astimezone())
        self._open = opener or _default_opener(str(cfg.base_url))
        self._sleep = sleep
        self._ctx = ssl.create_default_context()  # Windows: nạp kho chứng chỉ hệ thống (proxy TLS công ty)

    @property
    def match_id(self) -> str:
        return self._match_id

    # ───── API ─────
    def snapshot(self) -> MatchSnapshot:
        # A cached serverTime must never be treated as a fresh clock sample.
        status, body, _ = self._call(
            "GET", self._url("match"), headers={"Cache-Control": "no-cache"}, action="match"
        )
        if status == 304:
            raise GameMasterError("Snapshot was not refreshed; cached server time cannot authorize work")
        return parse_snapshot(body, match_id=self._match_id, local_now=self._now())

    def start_working(self, item_id: str) -> StartResult:
        _, body, _ = self._call(
            "POST", self._url("start_working", item_id=item_id), payload={}, action="start_working"
        )
        return parse_start(body, item_id)

    def submit(
        self, item_id: str, *, git_revision: str, answer: dict[str, Any], idempotency_key: str
    ) -> SubmitResult:
        payload = {"gitRevision": git_revision, "answer": answer}
        _, body, _ = self._call(
            "POST",
            self._url("submit", item_id=item_id),
            payload=payload,
            headers={"Idempotency-Key": idempotency_key},
            action="submit",
        )
        return parse_submit(body, item_id)

    def submission(self, item_id: str, submission_id: str) -> GradeResult:
        url = self._url("submission", item_id=item_id, submission_id=submission_id)
        _, body, _ = self._call("GET", url, action="submission")
        return parse_grade(body, item_id, submission_id)

    # ───── nội bộ ─────
    def _url(self, name: str, **params: str) -> str:
        values = {"match_id": self._match_id, **params}
        path = self._cfg.paths[name].format(**{k: quote(str(v), safe="") for k, v in values.items()})
        return str(self._cfg.base_url).rstrip("/") + path

    def _headers(self, extra: Mapping[str, str], action: str) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "ddt-digital-project-team"}
        if self._token and action in self._cfg.bearer_actions:
            h["Authorization"] = f"Bearer {self._token}"
        if self._cfg.team_header and self._team:
            h[self._cfg.team_header] = self._team
        h.update(extra)
        return h

    def _call(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        action: str = "",
    ) -> tuple[int, dict[str, Any], Mapping[str, str]]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        hdrs = self._headers(headers or {}, action)
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)  # noqa: S310 — https cấu hình
            try:
                with self._open(req, timeout=self._cfg.timeout_s, context=self._ctx) as resp:
                    raw = resp.read().decode("utf-8") or "{}"
                    return int(resp.status), _json(raw), dict(resp.headers)
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return 304, {}, dict(e.headers or {})
                detail = _safe_body(e)
                if method == "GET" and e.code in _RETRY_STATUS and attempt < self._cfg.retries:
                    attempt += 1
                    self._sleep(_retry_after(e.headers, attempt))
                    continue
                raise GameMasterError(
                    f"{method} {_path_of(url)} → HTTP {e.code}: {detail[:300]}",
                    status=e.code,
                    hint="401/403: kiểm token (gamemaster.token_env) · 409/422: đã nộp hoặc chưa start working",
                ) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if method == "GET" and attempt < self._cfg.retries:
                    attempt += 1
                    self._sleep(min(0.5 * 2**attempt, 8.0))
                    continue
                raise GameMasterError(f"{method} {_path_of(url)}: không kết nối được ({e})") from e


def _default_opener(base_url: str) -> Callable[..., Any]:
    """Game Master thật: proxy hệ thống (máy công ty). Loopback (bản giả lập): KHÔNG qua proxy."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def open_direct(req: Any, *, timeout: float, context: Any) -> Any:
            return direct.open(req, timeout=timeout)

        return open_direct
    return urllib.request.urlopen  # noqa: S310 — base_url đã kiểm chỉ http(s)


def _json(raw: str) -> dict[str, Any]:
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GameMasterError(f"Game Master trả không phải JSON: {raw[:200]}") from e
    if not isinstance(d, dict):
        raise GameMasterError("Game Master response must be a JSON object")
    return d


def _safe_body(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _retry_after(headers: Any, attempt: int) -> float:
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return min(float(raw), 30.0) if raw is not None else min(0.5 * 2.0**attempt, 8.0)
    except (TypeError, ValueError):
        return min(0.5 * 2.0**attempt, 8.0)


def _path_of(url: str) -> str:
    """Log đường dẫn, không log host/query (có thể chứa thông tin đội)."""
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]

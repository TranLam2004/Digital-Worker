"""Kiểm URL / truy vấn cho tool mạng (web.* · docs.*) — dùng ở policy (lớp 1, có log TOOL_DENIED) và trong tool
(mỗi bước redirect). Không gọi mạng, trừ phân giải DNS để chặn IP nội bộ (SSRF).

Quy tắc: chỉ https · host khớp allowlist (chính nó hoặc subdomain) · URL/truy vấn không chứa chuỗi giống secret ·
không vượt độ dài · IP đích không phải private / loopback / link-local / reserved.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from digital_development_team.config.models import NetworkCfg, WebHostCfg
from digital_development_team.policy.hooks import redact_secrets

Resolver = Callable[[str], list[str]]


@dataclass(frozen=True, slots=True)
class NetVerdict:
    ok: bool
    rule: str = ""
    reason: str = ""
    trust: str = "docs"


def host_entry(host: str, cfg: NetworkCfg) -> WebHostCfg | None:
    h = host.lower().rstrip(".")
    for e in cfg.allow:
        allowed = e.host.lower().lstrip("*.").rstrip(".")
        if h == allowed or h.endswith("." + allowed):
            return e
    return None


def check_url(url: str, cfg: NetworkCfg) -> NetVerdict:
    if not cfg.web_enabled:
        return NetVerdict(False, "net:disabled", "web access is disabled (policy.network.web_enabled)")
    if len(url) > cfg.max_url_chars:
        return NetVerdict(False, "net:url_too_long", f"URL longer than {cfg.max_url_chars} characters")
    parts = urlsplit(url)
    if parts.scheme != "https":
        return NetVerdict(False, "net:scheme", "only https:// URLs are allowed")
    if parts.username or parts.password:
        return NetVerdict(False, "net:userinfo", "credentials in the URL are not allowed")
    if redact_secrets(url)[1]:
        return NetVerdict(False, "net:secret_in_url", "the URL contains something that looks like a secret")
    host = parts.hostname or ""
    entry = host_entry(host, cfg)
    if entry is None:
        return NetVerdict(
            False,
            "net:host_not_allowed",
            f"{host or '?'} is not in policy.network.allow; use docs.lookup or an allowed documentation site",
        )
    return NetVerdict(True, trust=entry.trust)


def check_query(query: str, cfg: NetworkCfg) -> NetVerdict:
    if not cfg.web_enabled:
        return NetVerdict(False, "net:disabled", "web access is disabled (policy.network.web_enabled)")
    if len(query) > cfg.max_query_chars:
        return NetVerdict(False, "net:query_too_long", f"query longer than {cfg.max_query_chars} characters")
    if redact_secrets(query)[1]:
        return NetVerdict(
            False, "net:secret_in_query", "the query contains something that looks like a secret"
        )
    return NetVerdict(True)


def _default_resolver(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def public_ip_ok(host: str, cfg: NetworkCfg, resolver: Resolver | None = None) -> NetVerdict:
    """Mọi địa chỉ host phân giải ra phải là IP công cộng (chặn DNS trỏ về mạng nội bộ)."""
    if not cfg.block_private_ips:
        return NetVerdict(True)
    try:
        addrs = (resolver or _default_resolver)(host)
    except OSError as e:
        return NetVerdict(False, "net:dns", f"cannot resolve {host}: {e}")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return NetVerdict(False, "net:private_ip", f"{host} resolves to a non-public address")
    return NetVerdict(True)

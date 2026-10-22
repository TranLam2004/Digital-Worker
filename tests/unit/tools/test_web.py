"""Phase 4: tra cứu khi KB trống — netguard, web.fetch / web.search, docs.lookup, llm.ask, skill.search/load."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from digital_development_team.config.models import NetworkCfg
from digital_development_team.core.context import Actor, CancelToken, SystemClock, ToolContext
from digital_development_team.core.enums import Stage
from digital_development_team.policy.netguard import check_query, check_url, host_entry, public_ip_ok
from digital_development_team.tools.docs import DocsTools, detect_ecosystem
from digital_development_team.tools.llm_ask import LlmAskTools
from digital_development_team.tools.web import SearchBackend, WebTools, html_to_text
from tests.unit.tools.test_tools import _dispatch, env  # noqa: F401  (fixture)

CFG = NetworkCfg()


def _ctx(worktree: Path | None = None, stage: Stage = Stage.IMPLEMENT) -> ToolContext:
    return ToolContext(
        issue_id="DDT-0001",
        actor=Actor(role="dev"),
        stage=stage,
        worktree=str(worktree) if worktree else None,
        home=".",
        allowed_files=(),
        cancel=CancelToken(),
        clock=SystemClock(),
    )


def _public(_host: str) -> list[str]:
    return ["93.184.216.34"]


# ───── netguard ─────
def test_url_and_query_rules() -> None:
    assert check_url("https://docs.python.org/3/library/decimal.html", CFG).ok
    assert check_url("https://foo.readthedocs.io/en/latest/", CFG).ok  # subdomain
    assert check_url("https://github.com/x/y", CFG).trust == "ugc"
    assert check_url("http://docs.python.org/3/", CFG).rule == "net:scheme"
    assert check_url("https://evil.example.com/", CFG).rule == "net:host_not_allowed"
    assert check_url("https://user:pw@docs.python.org/", CFG).rule == "net:userinfo"
    assert check_url("https://docs.python.org/?k=" + "x" * 600, CFG).rule == "net:url_too_long"
    assert check_url("https://docs.python.org/?token=ghp_" + "a" * 36, CFG).rule == "net:secret_in_url"
    assert host_entry("notdocs.python.org.evil.com", CFG) is None
    assert check_query("python decimal ROUND_HALF_EVEN", CFG).ok
    assert check_query("q" * 300, CFG).rule == "net:query_too_long"
    assert public_ip_ok("internal", CFG, lambda _h: ["10.0.0.5"]).rule == "net:private_ip"
    assert public_ip_ok("lo", CFG, lambda _h: ["127.0.0.1"]).rule == "net:private_ip"
    assert public_ip_ok("ok", CFG, _public).ok


def test_html_to_text_keeps_structure() -> None:
    title, text = html_to_text(
        "<html><head><title>Decimal</title><script>evil()</script></head>"
        "<body><h2>quantize</h2><p>Rounds using <code>ROUND_HALF_EVEN</code>.</p></body></html>"
    )
    assert title == "Decimal" and "## quantize" in text and "ROUND_HALF_EVEN" in text and "evil" not in text


# ───── fetch ─────
def _web(handler: Any, tmp_path: Path, ask: Any = None) -> tuple[WebTools, list[str]]:
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return handler(req)  # type: ignore[no-any-return]

    w = WebTools(
        CFG, cache_dir=tmp_path / "cache", ask=ask, transport=httpx.MockTransport(h), resolver=_public
    )
    return w, calls


def test_fetch_page_redirects_cache_and_question(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/3/library/decimal.html"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<title>decimal</title><p>ROUND_HALF_EVEN is the default.</p>",
        )

    asked: list[str] = []
    w, calls = _web(
        handler, tmp_path, ask=lambda s, p, n: asked.append(p) or "Quote: 'ROUND_HALF_EVEN is the default.'"
    )
    r = w.fetch({"url": "https://docs.python.org/old"}, _ctx(), None)
    assert r.status == "ok" and r.structured["url"].endswith("/3/library/decimal.html")
    assert "ROUND_HALF_EVEN" in r.structured["text"] and r.structured["trust"] == "docs" and len(calls) == 2
    again = w.fetch({"url": "https://docs.python.org/old"}, _ctx(), None)
    assert again.status == "ok" and len(calls) == 2  # cache
    q = w.fetch({"url": "https://docs.python.org/old", "question": "default rounding?"}, _ctx(), None)
    assert q.structured["answer"].startswith("Quote") and "<untrusted_input>" in asked[0]


def test_fetch_blocks_bad_redirects_types_and_size(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/escape":
            return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})
        if req.url.path == "/bin":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\0\1")
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"x" * (CFG.max_bytes + 10)
        )

    w, _ = _web(handler, tmp_path)
    assert w.fetch({"url": "https://docs.python.org/escape"}, _ctx(), None).status == "denied"
    assert w.fetch({"url": "https://docs.python.org/bin"}, _ctx(), None).error.code == "NOT_TEXT"
    assert w.fetch({"url": "https://docs.python.org/big"}, _ctx(), None).error.code == "TOO_LARGE"


# ───── search ─────
def test_search_marks_fetchable_and_falls_back(tmp_path: Path) -> None:
    def broken(q: str, n: int, prefer: Any, cancel: Any) -> list[dict[str, str]]:
        raise RuntimeError("quota")

    def ok(q: str, n: int, prefer: Any, cancel: Any) -> list[dict[str, str]]:
        assert "docs.python.org" in prefer
        return [
            {"title": "decimal", "url": "https://docs.python.org/3/library/decimal.html", "snippet": "..."},
            {"title": "blog", "url": "https://random-blog.example/decimal", "snippet": "..."},
        ]

    w = WebTools(CFG, backends=[SearchBackend("a", broken), SearchBackend("b", ok)])
    r = w.search({"query": "python decimal rounding"}, _ctx(), None)
    assert r.status == "ok" and r.structured["backend"] == "b"
    assert [x["fetchable"] for x in r.structured["results"]] == [True, False]
    none = WebTools(CFG).search({"query": "python decimal"}, _ctx(), None)
    assert none.status == "error" and none.error is not None and none.error.code == "NOT_AVAILABLE"


# ───── docs.lookup ─────
def test_docs_prefers_installed_package_then_registry(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    mod = wt / "node_modules" / "leftpad"
    mod.mkdir(parents=True)
    (wt / "package.json").write_text("{}", encoding="utf-8")
    (mod / "package.json").write_text(json.dumps({"version": "1.3.0"}), encoding="utf-8")
    (mod / "README.md").write_text("# leftpad\nleftpad(str, len, ch) pads on the left.", encoding="utf-8")
    assert detect_ecosystem(wt) == "javascript"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            text=json.dumps(
                {
                    "dist-tags": {"latest": "1.4.0"},
                    "readme": "registry readme",
                    "homepage": "https://github.com/x/leftpad",
                }
            ),
        )

    web, _ = _web(handler, tmp_path)
    asked: list[str] = []
    docs = DocsTools(
        web, sandbox=None, sandbox_cfg=None, ask=lambda s, p, n: asked.append(p) or "pads on the left"
    )
    r = docs.lookup({"package": "leftpad"}, _ctx(wt), None)
    assert r.status == "ok" and r.structured["version"] == "1.3.0"  # bản đã cài thắng bản mới nhất
    assert (
        r.structured["sources"][0].startswith("installed leftpad@1.3.0")
        and "registry readme" in r.structured["text"]
    )
    q = docs.lookup({"package": "leftpad", "question": "which side?"}, _ctx(wt), None)
    assert q.structured["answer"] == "pads on the left" and "leftpad(str, len, ch)" in asked[0]
    bad = docs.lookup({"package": "rm -rf /"}, _ctx(wt), None)
    assert bad.status == "error"


# ───── llm.ask ─────
def test_llm_ask_limits_per_stage() -> None:
    roles: list[str] = []
    t = LlmAskTools(lambda role, s, p, n: roles.append(role) or "answer")
    for _ in range(5):
        assert t.ask({"question": "regex for semver?"}, _ctx(), None).status == "ok"
    assert t.ask({"question": "one more?"}, _ctx(), None).error.code == "LIMIT"
    assert t.ask({"question": "other stage", "mode": "quick"}, _ctx(stage=Stage.SPEC), None).status == "ok"
    so = t.ask({"question": "why does my fix fail?", "mode": "second_opinion"}, _ctx(), None)
    assert so.status == "ok" and roles[-1] == "llm_second_opinion"
    assert t.ask({"question": "again?", "mode": "second_opinion"}, _ctx(), None).error.code == "LIMIT"


# ───── qua Dispatcher + policy thật ─────
def test_policy_and_skill_tools_through_dispatcher(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    rt, tooling, agents, wt = env
    r = _dispatch(rt, tooling, agents, wt, "dev", "web.fetch", url="https://evil.example.com/x")
    assert r.status == "denied" and r.structured["rule"] == "net:host_not_allowed"
    r = _dispatch(rt, tooling, agents, wt, "dev", "web.fetch", url="http://docs.python.org/3/")
    assert r.status == "denied" and r.structured["rule"] == "net:scheme"
    r = _dispatch(rt, tooling, agents, wt, "verifier", "web.fetch", url="https://docs.python.org/3/")
    assert r.status == "denied"  # Verifier không có web: kiểm chứng độc lập
    r = _dispatch(rt, tooling, agents, wt, "dev", "skill.search", query="debug a failing test root cause")
    top2 = [x["name"] for x in r.structured["results"][:2]]
    assert r.status == "ok" and "systematic-debugging" in top2  # pi-investigation cũng nói về root cause
    r = _dispatch_args(rt, tooling, agents, wt, "verifier", "skill.load", {"name": "py-review"})
    assert r.status == "ok" and "## Procedure" in r.structured["body"]
    r = _dispatch_args(rt, tooling, agents, wt, "dev", "skill.load", {"name": "no-such-skill"})
    assert r.status == "error" and r.error is not None and r.error.code == "NOT_FOUND"


def _dispatch_args(
    rt: Any, tooling: Any, agents: Any, wt: Path, role: str, tool: str, args: dict[str, Any]
) -> Any:
    """Như _dispatch nhưng args là dict (tool có tham số trùng tên với tham số của helper, vd `name`)."""
    return _call(rt, tooling, agents, wt, role, tool, args)


def _call(rt: Any, tooling: Any, agents: Any, wt: Path, role: str, tool: str, args: dict[str, Any]) -> Any:
    from digital_development_team.core.enums import Tier
    from digital_development_team.core.models import ToolCall
    from digital_development_team.policy.engine import PolicyContext

    spec = agents.get(role)
    actor = Actor(role=role)
    call = ToolCall(id="c", name=tool, args=args, actor=actor, issue_id="DDT-0001", stage=Stage.IMPLEMENT)
    tctx = ToolContext(
        issue_id="DDT-0001",
        actor=actor,
        stage=Stage.IMPLEMENT,
        worktree=str(wt),
        home=str(rt.home),
        allowed_files=(),
        cancel=CancelToken(),
        clock=rt.clock,
    )
    return tooling.dispatcher.dispatch(
        call, spec, tctx, PolicyContext(tier=Tier.T1, worktree=str(wt), allowed_files=())
    )


@pytest.mark.parametrize("bad", ["", "ab"])
def test_search_query_schema(bad: str) -> None:
    w = WebTools(CFG, backends=[SearchBackend("x", lambda q, n, p, c: [])])
    assert w.search({"query": bad}, _ctx(), None).status in ("ok", "denied")

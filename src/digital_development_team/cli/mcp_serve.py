"""`ddt mcp --session FILE` — MCP server (stdio) do `claude -p --mcp-config` / `codex exec -c mcp_servers.ddt.*` spawn. Expose tool của agent qua Dispatcher.

Cùng policy 4 lớp · cùng allowlist frontmatter · cùng events.jsonl (khoá file liên process) như transport API.
stdout là kênh JSON-RPC — KHÔNG print gì ra stdout; log ra stderr.
Tool `policy` = --permission-prompt-tool: Dispatcher đã quyết allow/deny nên luôn trả allow (lớp chặn thật nằm ở dispatch).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from digital_development_team.llm.claude_cli_provider import PERMISSION_TOOL
from digital_development_team.llm.cli_session import CliSession

app = typer.Typer()
log = logging.getLogger("ddt.mcp")


def build_handlers(
    session: CliSession,
) -> tuple[Callable[[], list[dict[str, Any]]], Callable[[str, dict[str, Any]], str]]:
    """(list_tools, call_tool) thuần Python — test được không cần stdio. Lắp runtime + tooling từ session."""
    from digital_development_team.agents import AgentRegistry
    from digital_development_team.app import build_runtime, build_tooling, mcp_grants
    from digital_development_team.core.context import Actor, CancelToken, ToolContext
    from digital_development_team.core.enums import Stage, Tier
    from digital_development_team.core.errors import ConfigError
    from digital_development_team.core.models import ToolCall
    from digital_development_team.llm.toolnames import to_canonical
    from digital_development_team.policy.engine import PolicyContext

    rt = build_runtime(
        Path(session.home),
        config_dir=Path(session.config_dir) if session.config_dir else None,
        repo=Path(session.repo) if session.repo else None,
        preset=session.preset,
        flags=session.flags,
    )

    def base_ref(tctx: Any) -> str:
        cp = rt.checkpoints.load(tctx.issue_id)
        return cp.base_commit if cp and cp.base_commit else "main"

    tooling = build_tooling(rt, base_ref=base_ref)
    stage = Stage(session.stage)
    registry = AgentRegistry(rt.resolver)
    registry.grant(mcp_grants(rt))  # cùng grant MCP như runtime → transport CLI thấy đúng tool đó
    registry.revoke(rt.config.policy.tool_denylist)
    spec = registry.get(session.role).for_stage(stage)  # vd Dev ở ANSWER: hồ sơ chỉ đọc
    actor = Actor(role=session.role)
    tctx = ToolContext(
        issue_id=session.issue_id,
        actor=actor,
        stage=stage,
        worktree=session.worktree,
        home=session.home,
        allowed_files=tuple(session.allowed_files),
        cancel=CancelToken(),
        clock=rt.clock,
    )
    pctx = PolicyContext(
        tier=Tier(session.tier),
        worktree=session.worktree,
        allowed_files=tuple(session.allowed_files),
        spec_values=dict(session.spec_values),
        autonomy=session.autonomy,
    )
    permission_name = PERMISSION_TOOL.rsplit("__", 1)[-1]

    def list_tools() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in tooling.catalog.specs_for(list(session.tools or spec.tool_names)):
            out.append({"name": t.wire_name, "description": t.description, "inputSchema": t.input_schema})
        if not session.permission_tool:  # Codex: approval_policy=never, không có permission prompt tool
            return out
        out.append(
            {
                "name": permission_name,
                "description": "Permission prompt của Claude Code — policy DDT đã quyết trong từng tool, luôn allow.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string"},
                        "input": {"type": "object"},
                        "tool_use_id": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            }
        )
        return out

    def call_tool(name: str, arguments: dict[str, Any]) -> str:
        if name == permission_name and session.permission_tool:
            return json.dumps(
                {"behavior": "allow", "updatedInput": arguments.get("input") or {}}, ensure_ascii=False
            )
        try:
            canonical = to_canonical(name)
        except ConfigError as e:
            return json.dumps(
                {"status": "error", "code": "UNKNOWN_TOOL", "message": e.message}, ensure_ascii=False
            )
        call = ToolCall(
            id=f"mcp-{name}",
            name=canonical,
            args=dict(arguments),
            actor=actor,
            issue_id=session.issue_id,
            stage=stage,
        )
        result = tooling.dispatcher.dispatch(call, spec, tctx, pctx)
        return str(result.to_model_text())

    return list_tools, call_tool


async def serve(session: CliSession) -> None:
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    list_tools, call_tool = build_handlers(session)
    server: Server[Any, Any] = Server("ddt")

    @server.list_tools()  # type: ignore[untyped-decorator,no-untyped-call]
    async def _list() -> list[types.Tool]:
        return [
            types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
            for t in list_tools()
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, call_tool, name, dict(arguments or {}))
        return [types.TextContent(type="text", text=text)]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


# Provider CLI spawn `python -m digital_development_team.cli.mcp_serve --session F` — chạy thẳng module này, KHÔNG
# qua `ddt` (khỏi nạp mọi nhóm lệnh typer): mỗi agent run một lần spawn, mỗi giây đều tính vào trận 40 phút.


def _serve_file(session: Path) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    sess = CliSession.load(session)
    log.info("ddt mcp: issue=%s role=%s worktree=%s", sess.issue_id, sess.role, sess.worktree)
    asyncio.run(serve(sess))


@app.command("mcp")
def mcp_(
    ctx: typer.Context, session: Annotated[Path, typer.Option("--session", exists=True, dir_okay=False)]
) -> None:
    """MCP server stdio cho transport CLI (claude -p / codex exec spawn; không gọi tay)."""
    _serve_file(session)


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="ddt-mcp", description="MCP server stdio cho transport CLI")
    parser.add_argument("--session", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.session.is_file():
        parser.error(f"--session {args.session}: không phải file")
    _serve_file(args.session)


if __name__ == "__main__":
    main()

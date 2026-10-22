"""MCP server stdio tối giản cho test: echo · add · boom. MCP_DRIFT=1 → đổi mô tả echo (giả server nâng cấp)."""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back" + (" (v2)" if os.environ.get("MCP_DRIFT") == "1" else ""),
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    },
    {"name": "boom", "description": "Always fails", "inputSchema": {"type": "object", "properties": {}}},
]


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        continue  # notification
    if method == "initialize":
        reply(
            rid,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "1"},
            },
        )
    elif method == "tools/list":
        sys.stdout.write("not json noise\n")  # server in rác: client phải bỏ qua
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = req["params"]["name"], req["params"].get("arguments") or {}
        if name == "echo":
            reply(rid, {"content": [{"type": "text", "text": args.get("text", "")}]})
        elif name == "add":
            reply(
                rid,
                {
                    "content": [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}],
                    "structuredContent": {"sum": args.get("a", 0) + args.get("b", 0)},
                },
            )
        else:
            reply(rid, {"content": [{"type": "text", "text": "boom failed"}], "isError": True})
    else:
        reply(rid, error={"code": -32601, "message": f"unknown method {method}"})

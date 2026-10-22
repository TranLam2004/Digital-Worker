"""Binary `codex` giả cho test transport Codex CLI. Phát JSONL như `codex exec --json` 0.146. FAKE_CODEX_MODE:

  chat      → thread.started · turn.started · agent_message "echo:<prompt>" · turn.completed{usage}
  stream    → 2 mcp_tool_call (ddt.fs_read, ddt.blackboard_write) · agent_message "done" · turn.completed
  slow      → 20 mcp_tool_call, mỗi cái sleep 0.15s (test kill khi hết budget)
  limited   → error "You've hit your usage limit" · turn.failed, exit 1
  unauth    → error 401 Unauthorized · turn.failed, exit 1
  builtin   → 1 command_execution lọt qua (lẽ ra shell_tool đã tắt) · agent_message · turn.completed
Ghi argv nhận được vào $FAKE_CODEX_ARGV (JSON) và stdin vào $FAKE_CODEX_STDIN để test kiểm cờ.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time


def _ev(d: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def _usage(i: int, cached: int, o: int) -> dict[str, int]:
    return {
        "input_tokens": i,
        "cached_input_tokens": cached,
        "output_tokens": o,
        "reasoning_output_tokens": 3,
    }


def _tool(n: int, tool: str) -> None:
    item = {"id": f"item_{n}", "type": "mcp_tool_call", "server": "ddt", "tool": tool, "arguments": {}}
    _ev({"type": "item.started", "item": {**item, "status": "in_progress"}})
    _ev({"type": "item.completed", "item": {**item, "status": "completed", "result": {"content": []}}})


def main() -> int:
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CODEX_ARGV"):
        pathlib.Path(os.environ["FAKE_CODEX_ARGV"]).write_text(json.dumps(argv), encoding="utf-8")
    prompt = sys.stdin.read() if argv and argv[-1] == "-" else ""
    if os.environ.get("FAKE_CODEX_STDIN"):
        pathlib.Path(os.environ["FAKE_CODEX_STDIN"]).write_text(prompt, encoding="utf-8")
    mode = os.environ.get("FAKE_CODEX_MODE", "chat")
    _ev({"type": "thread.started", "thread_id": "thread-1"})
    _ev({"type": "turn.started"})
    if mode in ("limited", "unauth"):
        msg = (
            "You've hit your usage limit. Try again later."
            if mode == "limited"
            else "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
        )
        _ev({"type": "error", "message": f"Reconnecting... 1/5 ({msg})"})
        _ev({"type": "turn.failed", "error": {"message": msg}})
        sys.stderr.write(f"ERROR codex_api: {msg}\n")
        return 1
    if mode == "chat":
        _ev({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}})
        _ev(
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": f"echo:{prompt.strip()[:40]}"},
            }
        )
        _ev({"type": "turn.completed", "usage": _usage(150, 50, 7)})
        return 0
    if mode == "builtin":
        _ev(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "command_execution", "command": "ls", "status": "completed"},
            }
        )
        _ev({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "done"}})
        _ev({"type": "turn.completed", "usage": _usage(100, 0, 5)})
        return 0
    n = 20 if mode == "slow" else 2
    _ev(
        {
            "type": "item.completed",
            "item": {"id": "item_m", "type": "agent_message", "text": "reading the code"},
        }
    )
    for i in range(1, n + 1):
        _tool(i, "blackboard_write" if (mode == "stream" and i == n) else "fs_read")
        if mode == "slow":
            time.sleep(0.15)
    _ev({"type": "item.completed", "item": {"id": "item_z", "type": "agent_message", "text": "done"}})
    _ev({"type": "turn.completed", "usage": _usage(2400, 400, 100)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

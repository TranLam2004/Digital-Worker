"""Binary `claude` giả cho test transport CLI. Hành vi theo biến môi trường FAKE_CLAUDE_MODE:

  json      → in 1 JSON dict như `--output-format json`
  stream    → in stream-json: rate_limit_event · system/init · 2 assistant · result
  slow      → 20 assistant, mỗi lượt sleep 0.15s (test kill khi hết budget)
  limited   → rate_limit_event status=rejected + result is_error 429
  fail      → exit 1, stderr "Not logged in"
Ghi argv nhận được vào $FAKE_CLAUDE_ARGV (JSON) để test kiểm cờ.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time


def _usage(i: int, o: int, cr: int = 0, cc: int = 0) -> dict[str, int]:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cc,
    }


def _assistant(
    text: str | None, tools: list[str], usage: dict[str, int], stop: str, mid: str = "msg_1"
) -> str:
    content: list[dict[str, object]] = []
    if text:
        content.append({"type": "text", "text": text})
    for t in tools:
        content.append({"type": "tool_use", "id": "toolu_1", "name": t, "input": {"path": "calc.py"}})
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": mid,
                "model": "claude-fake",
                "role": "assistant",
                "content": content,
                "stop_reason": stop,
                "usage": usage,
            },
        }
    )


def main() -> int:
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_ARGV"):
        with pathlib.Path(os.environ["FAKE_CLAUDE_ARGV"]).open("w", encoding="utf-8") as f:
            json.dump(argv, f)
    mode = os.environ.get("FAKE_CLAUDE_MODE", "json")
    if os.environ.get("FAKE_CLAUDE_ALIAS_ONLY") and "--model" in argv:
        m = argv[argv.index("--model") + 1]
        if m.startswith("claude-"):  # như CLI thật 10/2026: ID pin cũ bị từ chối, alias họ model được nhận
            sys.stderr.write('[claude-code:unrecognized_model] {"model":"' + m + '","query_source":"sdk"}\n')
            return 1
    out = sys.stdout
    if mode == "fail":
        sys.stderr.write("Not logged in. Please run `claude` to login.\n")
        return 1
    if mode == "json":
        prompt = sys.stdin.read().strip()
        d = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": f"echo:{prompt[:40]}",
            "stop_reason": "end_turn",
            "session_id": "sess-1",
            "num_turns": 1,
            "total_cost_usd": 0.0,
            "usage": _usage(100, 7, cr=50),
            "modelUsage": {"claude-fake": {}},
            "permission_denials": [],
        }
        out.write(json.dumps(d) + "\n")
        return 0
    status = "rejected" if mode == "limited" else "allowed"
    out.write(
        json.dumps(
            {"type": "rate_limit_event", "rate_limit_info": {"status": status, "rateLimitType": "five_hour"}}
        )
        + "\n"
    )
    out.write(json.dumps({"type": "system", "subtype": "init", "tools": [], "model": "claude-fake"}) + "\n")
    out.flush()
    if mode == "limited":
        out.write(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "You've hit your usage limit",
                    "num_turns": 0,
                    "api_error_status": 429,
                }
            )
            + "\n"
        )
        return 1
    n = 20 if mode == "slow" else 2
    for i in range(1, n + 1):
        last = i == n
        mid = f"msg_{i}"
        if not last:  # như CLI thật: block text rồi block tool_use, 2 dòng cùng id, usage lặp
            out.write(_assistant("thinking…", [], _usage(1000, 10, cr=200), None, mid) + "\n")
        out.write(
            _assistant(
                "done" if last else None,
                [] if last else ["mcp__ddt__fs_read"],
                _usage(1000, 50, cr=200),
                "end_turn" if last else "tool_use",
                mid,
            )
            + "\n"
        )
        out.flush()
        if mode == "slow":
            time.sleep(0.15)
    out.write(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "num_turns": n,
                "usage": _usage(1000 * n, 50 * n, cr=200 * n),
                "permission_denials": [],
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

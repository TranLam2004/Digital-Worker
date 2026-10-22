"""Live: gọi `claude -p` thật (subscription). Chạy: pytest -m live tests/live/test_claude_cli_live.py"""

from __future__ import annotations

import shutil

import pytest

from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken
from digital_development_team.core.llm import ChatRequest, Message
from digital_development_team.llm.registry import ModelRegistry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(shutil.which("claude") is None, reason="không có Claude Code CLI"),
]


def test_complete_mode_pong() -> None:
    cfg = load_config(preset="cli_all")
    reg = ModelRegistry(cfg.models, env={})
    client = reg.client("llm_ask")
    assert client.ref.provider == "anthropic_cli"
    out = client.chat(
        ChatRequest(
            model=client.ref, system="Trả lời đúng một từ.", messages=(Message.user_text("Nói: pong"),)
        ),
        cancel=CancelToken(),
    )
    assert "pong" in out.response.text.lower() and out.cost_usd == 0.0 and out.response.usage.tokens_out > 0

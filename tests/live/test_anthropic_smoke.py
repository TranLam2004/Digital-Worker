"""Gọi LLM thật. Chạy: pytest -m live (cần ANTHROPIC_API_KEY). Mặc định bị loại bởi addopts."""

from __future__ import annotations

import os

import pytest

from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken
from digital_development_team.core.llm import ChatRequest, Message, StopReason, ToolSpec
from digital_development_team.llm import ModelRegistry

pytestmark = pytest.mark.live


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="cần ANTHROPIC_API_KEY")
def test_anthropic_smoke_text_and_tool() -> None:
    reg = ModelRegistry(load_config().models)
    client = reg.client("llm_ask")
    out = client.chat(
        ChatRequest(
            model=client.ref,
            system="Trả lời cực ngắn.",
            messages=(Message.user_text("Nói 'pong'"),),
            max_tokens=16,
            metadata={"role": "llm_ask", "issue": "live"},
        ),
        cancel=CancelToken().child(timeout_s=60),
    )
    assert "pong" in out.response.text.lower() and out.response.usage.tokens_out > 0 and out.cost_usd > 0

    tool = ToolSpec(
        name="fs.read",
        wire_name="fs_read",
        description="Đọc file trong repo",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    out2 = client.chat(
        ChatRequest(
            model=client.ref,
            system="Bạn là dev. Luôn dùng tool để đọc file trước khi trả lời.",
            messages=(Message.user_text("Đọc file calc.py"),),
            tools=(tool,),
            max_tokens=256,
            metadata={"role": "llm_ask", "issue": "live"},
        ),
        cancel=CancelToken().child(timeout_s=60),
    )
    assert out2.response.stop is StopReason.TOOL_USE and out2.response.tool_calls[0].name == "fs_read"

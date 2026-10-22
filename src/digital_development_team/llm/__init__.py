"""LLM — provider trung lập. Agent chỉ biết `LLMClient.chat(req)`; chọn model/provider là việc của models.yaml.

    reg = ModelRegistry(cfg.models, provider_override="mock")
    client = reg.client("dev")                 # resolve alias → ModelRef (+ fallback khác họ nếu cần)
    out = client.chat(req, cancel=cancel)      # retry/backoff · rate limit · fallback · cost
    out.response.text · out.cost_usd · out.ref.model

Adapter: anthropic_provider · openai_provider (openai + openai_compatible + azure) · mock (golden/echo).
gemini/bedrock: TODO(P2) — registry raise ProviderUnavailable có hint.
"""

from __future__ import annotations

from digital_development_team.llm.mock import MockProvider, RecordingProvider, ScriptStep
from digital_development_team.llm.registry import ChatResult, LLMClient, ModelRegistry, Resolution
from digital_development_team.llm.toolnames import to_canonical, to_wire

__all__ = [
    "ChatResult",
    "LLMClient",
    "MockProvider",
    "ModelRegistry",
    "RecordingProvider",
    "Resolution",
    "ScriptStep",
    "to_canonical",
    "to_wire",
]

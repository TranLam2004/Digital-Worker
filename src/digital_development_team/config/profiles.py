"""Separate Codex runtime profiles; model IDs and spending ceilings are explicit inputs."""

from __future__ import annotations

from typing import Any

from digital_development_team.core.errors import ConfigError


def codex_profile(
    mode: str,
    *,
    fast_model: str,
    main_model: str,
    strong_model: str,
    budget_usd: float,
    tokens: int = 1_000_000,
) -> dict[str, Any]:
    if mode not in ("daily", "contest") or budget_usd <= 0 or tokens <= 0:
        raise ConfigError("Choose daily|contest and positive budget/token ceilings")
    if not all(m.strip().startswith("gpt-") for m in (fast_model, main_model, strong_model)):
        raise ConfigError("Supply exact GPT model IDs available to your Codex account")
    aliases = {
        "haiku": (fast_model, "low"),
        "sonnet": (main_model, "medium"),
        "sonnet_lite": (main_model, "low"),
        "opus": (strong_model, "medium"),
        "fable": (strong_model, "high"),
    }
    return {
        "preset": "codex_all",
        "flags": ["--match", "--no-delegate", "--no-clarify"] if mode == "contest" else [],
        "models": {
            "aliases": {
                name: {"provider": "codex_cli", "family": "gpt", "model": model, "effort": effort}
                for name, (model, effort) in aliases.items()
            },
            "roles": {
                "dev": {"model": "sonnet", "fallback": "opus"},
                "dev_rework": {"model": "opus", "fallback": "sonnet"},
            },
        },
        "policy": {
            "wip_limit": 1,
            "autonomy": "autonomous" if mode == "contest" else "conservative",
            "no_learn": mode == "contest",
            "gamemaster": {
                "http_contract_confirmed": False,
                "bearer_actions": [],
                "pi_slot": True,
                "final_grade_timeout_s": 300,
                "retry_backoff_s": 15,
            },
        },
        "budgets": {
            "session": {
                "tokens": tokens,
                "usd": budget_usd,
                "warn_at": 0.60,
                "economy_at": 0.75,
                "stop_new_at": 0.85,
            }
        },
    }

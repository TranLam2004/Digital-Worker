"""AgentLoop với MockProvider script: tool_write (Dev thật trên worktree) · final_message · blocked · budget · nudge."""

from __future__ import annotations

from pathlib import Path

import pytest

from digital_development_team.agents.brief import BriefInputs
from digital_development_team.agents.outcome import blocked_reason, extract_json_object
from digital_development_team.app import Runtime, build_runtime
from digital_development_team.budget import resolve_stage_budget
from digital_development_team.core.context import Actor, CancelToken, FakeClock, RunContext
from digital_development_team.core.enums import EventType, PermissionMode, Stage, Tier
from digital_development_team.core.errors import ContextOverflow, ProviderPaused, RateLimited
from digital_development_team.eventlog.reader import read_events
from digital_development_team.llm.mock import MockProvider, ScriptStep
from digital_development_team.runner.context import Services
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo
from tests.unit.runner.test_runner import _runner

ISSUE = "DDT-0001"
GOLDEN = TESTDATA / "golden" / "calc_mean"


def test_extract_and_blocked() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('Đây là kết quả:\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('prefix {"a": 3} suffix') == {"a": 3}
    assert extract_json_object("issue_id: DDT-0001\ntype: bug\n") == {"issue_id": "DDT-0001", "type": "bug"}
    assert extract_json_object("văn xuôi") is None
    assert blocked_reason({"status": "blocked", "reason": "no test runner"}, "") == "no test runner"
    assert blocked_reason(None, "BLOCKED: không build được") == "không build được"
    assert blocked_reason({"status": "ok"}, "x") is None


@pytest.fixture
def env(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock):  # type: ignore[no-untyped-def]
    _, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN)
    _runner(rt).run(ISSUE, cancel=CancelToken(), until=Stage.SPEC)
    svc = rt.services()
    yield rt, svc
    rt.close()


def _run(
    rt: Runtime,
    svc: Services,
    role: str,
    stage: Stage,
    *,
    worktree: str | None,
    mock: MockProvider | None = None,
) -> tuple:  # type: ignore[type-arg]
    assert svc.agents and svc.brief_builder and svc.agent_runtime
    if mock is not None:
        rt.models.register_provider("mock", mock)
    spec = svc.agents.get(role)
    ref = resolve_stage_budget(rt.config.budgets, stage, Tier.T1)
    assert ref is not None
    allowed = tuple(rt.store.read(ISSUE, "spec.yaml").content["allowed_files"])
    brief = svc.brief_builder.build(
        spec, BriefInputs(issue_id=ISSUE, stage=stage, tier=Tier.T1, budget=ref, facts={})
    )
    rctx = RunContext(
        issue_id=ISSUE,
        role=role,
        stage=stage,
        tier=Tier.T1,
        budget=ref,
        cancel=CancelToken(),
        clock=rt.clock,
        actor=Actor(role=role),
        worktree=worktree,
        allowed_files=allowed,
        permission_mode=spec.permission_mode,
    )
    return svc.agent_runtime.run(spec, brief, rctx), spec


def test_dev_golden_writes_patch(env) -> None:  # type: ignore[no-untyped-def]
    rt, svc = env
    assert svc.worktrees is not None
    info = svc.worktrees.ensure(ISSUE)
    outcome, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=str(info.path))
    assert outcome.kind == "artifact" and outcome.artifact_uri == f"blackboard://{ISSUE}/patch/@1", outcome
    assert outcome.usage.turns == 4 and outcome.usage.tokens > 0
    patch = rt.store.read(ISSUE, "patch/").content
    assert "raise ValueError" in patch["diff"] and set(patch["files_touched"]) == {
        "calc.py",
        "tests/test_calc.py",
    }
    assert patch["head_commit"] != patch["base_commit"] and "5 passed" in patch["impl_log"]
    # worktree thật đã sửa + test pass
    assert "if not xs:" in (info.path / "calc.py").read_text(encoding="utf-8")
    types = [e.event for e in read_events(rt.layout, ISSUE)]
    assert (
        types.count(EventType.LLM_CALL) == 4
        and EventType.TOOL_CALL in types
        and EventType.SLOT_WRITTEN in types
    )
    llm = next(e for e in read_events(rt.layout, ISSUE) if e.event is EventType.LLM_CALL)
    assert (
        llm.llm is not None
        and llm.llm.model == "mock"
        and llm.budget is not None
        and llm.budget.turns_max == 25
    )
    tool_names = [e.tool.name for e in read_events(rt.layout, ISSUE) if e.tool]
    assert (
        tool_names[:2] == ["fs.read", "fs.read"]
        and "test.run" in tool_names
        and tool_names[-1] == "blackboard.write"
    )
    # golden hết lượt mà gọi tiếp → GoldenExhausted (loop đã return ngay khi slot ghi nên không chạm)
    outcome2, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=str(info.path))
    assert outcome2.kind == "blocked" and "golden" in (
        outcome2.reason or ""
    )  # GoldenExhausted → provider lỗi → blocked


def test_denied_tool_feeds_back_then_blocked(env) -> None:  # type: ignore[no-untyped-def]
    rt, svc = env
    mock = MockProvider(echo=False)
    mock.add_script(
        "dev",
        [
            ScriptStep(
                text="sửa file ngoài scope",
                tool_calls=[{"name": "fs_write", "input": {"path": "other.py", "content": "x"}}],
            ),  # type: ignore[list-item]
            ScriptStep(text='{"status": "blocked", "reason": "cần sửa other.py nhưng ngoài allowed_files"}'),
        ],
    )
    assert svc.worktrees is not None
    info = svc.worktrees.ensure(ISSUE)
    outcome, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=str(info.path), mock=mock)
    assert outcome.kind == "blocked" and "other.py" in (outcome.reason or "")
    # tool_result báo denied đã vào context lượt 2
    second = mock.calls[1]
    assert any(
        "POLICY_DENIED" in b.content
        for m in second.messages
        for b in m.blocks
        if hasattr(b, "content") and hasattr(b, "tool_use_id")
    )
    assert not (info.path / "other.py").exists()


def test_tool_write_nudge_then_blocked(env) -> None:  # type: ignore[no-untyped-def]
    rt, svc = env
    mock = MockProvider(echo=False)
    mock.add_script("dev", [ScriptStep(text="xong rồi"), ScriptStep(text="ừ xong")])
    assert svc.worktrees is not None
    info = svc.worktrees.ensure(ISSUE)
    outcome, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=str(info.path), mock=mock)
    assert outcome.kind == "blocked" and "không ghi patch/" in (outcome.reason or "")
    assert "without writing `patch/`" in mock.calls[1].messages[-1].text


def test_budget_final_turn(env) -> None:  # type: ignore[no-untyped-def]
    rt, svc = env
    mock = MockProvider(echo=False)
    big = {"tokens_in": 100_000, "tokens_out": 1000}
    mock.add_script(
        "dev",
        [
            ScriptStep(text="đọc", tool_calls=[{"name": "fs_read", "input": {"path": "calc.py"}}], usage=big),  # type: ignore[list-item,arg-type]
            ScriptStep(
                text="đọc nữa", tool_calls=[{"name": "fs_read", "input": {"path": "calc.py"}}], usage=big
            ),  # type: ignore[list-item,arg-type]
            ScriptStep(
                text="vẫn đọc", tool_calls=[{"name": "fs_read", "input": {"path": "calc.py"}}], usage=big
            ),  # type: ignore[list-item,arg-type]
        ],
    )
    assert svc.worktrees is not None
    info = svc.worktrees.ensure(ISSUE)
    outcome, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=str(info.path), mock=mock)
    # 150k cap: sau lượt 2 (202k) → lượt cuối được báo; lượt 3 vẫn gọi tool → circuit breaker
    assert outcome.kind == "blocked" and "hết budget" in (outcome.reason or "")
    assert any("FINAL turn" in m.text for m in mock.calls[2].messages)
    types = [e.event for e in read_events(rt.layout, ISSUE)]
    assert EventType.BUDGET_WARNING in types and EventType.CIRCUIT_BREAKER in types


def test_final_message_mode_schema_retry(env) -> None:  # type: ignore[no-untyped-def]
    rt, svc = env
    assert svc.agents is not None
    intake_spec = svc.agents.get("intake")
    assert intake_spec.permission_mode is PermissionMode.READONLY
    mock = MockProvider(echo=False)
    good = {
        "issue_id": ISSUE,
        "type": "bug",
        "severity": "P2",
        "complexity": "small",
        "risk_tier": "T1",
        "route": "standard",
        "confidence": 0.8,
        "affected_area": "calc",
        "rationale": "rõ",
    }
    mock.add_script(
        "intake",
        [
            ScriptStep(text="Tôi nghĩ đây là bug."),  # không phải JSON
            ScriptStep(text='{"issue_id": "DDT-0001", "type": "bug", "confidence": 2}'),  # sai schema
            ScriptStep(text=__import__("json").dumps(good)),
        ],
    )
    outcome, _ = _run(rt, svc, "intake", Stage.TRIAGE, worktree=None, mock=mock)
    assert outcome.kind == "artifact" and outcome.artifact_uri.endswith(
        "intake.yaml@2"
    )  # stub đã ghi v1 trước đó
    assert rt.store.read(ISSUE, "intake.yaml").content["confidence"] == 0.8
    texts = [m.text for m in mock.calls[2].messages if m.role == "user"]
    assert any("EXACTLY one JSON object" in t for t in texts) and any(
        "does not match the schema (2/3)" in t for t in texts
    )
    assert outcome.usage.turns == 3


def test_transient_provider_error_pauses_instead_of_blocking(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """LLMClient đã retry + fallback mà vẫn 429 → loop raise ProviderPaused; lỗi không retry được vẫn blocked."""
    rt, svc = env

    def down(self, req, *, cancel):  # type: ignore[no-untyped-def]
        raise RateLimited("429 usage limit")

    monkeypatch.setattr("digital_development_team.llm.registry.LLMClient.chat", down)
    with pytest.raises(ProviderPaused) as ei:
        _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=None)
    assert ei.value.code == "PROVIDER_PAUSED" and "429" in ei.value.message

    def overflow(self, req, *, cancel):  # type: ignore[no-untyped-def]
        raise ContextOverflow("prompt too long")

    monkeypatch.setattr("digital_development_team.llm.registry.LLMClient.chat", overflow)
    outcome, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=None)
    assert outcome.kind == "blocked" and "prompt too long" in (outcome.reason or "")


def test_unavailable_rework_model_falls_back_to_normal_model(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Rework dùng models.yaml#dev_rework; transport không có model đó (vd CLI subscription) → model thường, không BLOCKED."""
    import dataclasses

    from digital_development_team.agents.loop import AgentLoop
    from digital_development_team.core.models import AgentOutcome, BudgetUsage

    rt, svc = env
    seen: list[str] = []

    def fake(self, spec, brief, ctx, client, tw):  # type: ignore[no-untyped-def]
        seen.append(client.resolution.role)
        if client.resolution.role == "dev_rework":
            return AgentOutcome.blocked("claude_cli: CLI không nhận model claude-fable-5-1", BudgetUsage())
        return AgentOutcome.artifact("blackboard://DDT-0001/patch/@1", BudgetUsage())

    monkeypatch.setattr(AgentLoop, "_run_once", fake)
    assert svc.agents and svc.brief_builder and svc.agent_runtime
    spec = svc.agents.get("dev")
    ref = resolve_stage_budget(rt.config.budgets, Stage.IMPLEMENT, Tier.T1)
    assert ref is not None
    brief = svc.brief_builder.build(
        spec, BriefInputs(issue_id=ISSUE, stage=Stage.IMPLEMENT, tier=Tier.T1, budget=ref)
    )
    rctx = RunContext(
        issue_id=ISSUE,
        role="dev",
        stage=Stage.IMPLEMENT,
        tier=Tier.T1,
        budget=ref,
        cancel=CancelToken(),
        clock=rt.clock,
        actor=Actor(role="dev"),
    )
    out = svc.agent_runtime.run(spec, brief, dataclasses.replace(rctx, model_role="dev_rework"))
    assert seen == ["dev_rework", "dev"] and out.kind == "artifact"
    seen.clear()
    svc.agent_runtime.run(spec, brief, rctx)
    assert seen == ["dev"]

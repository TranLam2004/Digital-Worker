"""Phase 8: phát hiện kẹt (Dispatcher) · condenser tầng 2 · progress.yaml cho lần chạy sau và blocked_report."""

from __future__ import annotations

import json

from digital_development_team.agents.brief import BriefInputs
from digital_development_team.budget.resolve import resolve_stage_budget
from digital_development_team.condenser import CONDENSED, condense_tool_results
from digital_development_team.core.context import Actor
from digital_development_team.core.enums import EventType, Stage, Tier
from digital_development_team.core.llm import Message, TextBlock, ToolResultBlock, ToolUseBlock
from digital_development_team.core.models import ToolResult
from digital_development_team.eventlog.reader import read_events
from digital_development_team.llm.mock import MockProvider, ScriptStep
from digital_development_team.runner.recovery import build_blocked_report
from digital_development_team.tools.stuck import StuckDetector
from tests.unit.agents.test_loop import ISSUE, _run, env  # noqa: F401  (fixture)


def test_detector_repeat_denials_then_stop() -> None:
    d = StuckDetector()
    same = ToolResult.fail("NOT_FOUND", "no such file")
    assert d.observe("k", "fs.read", "h1", same) is None
    assert d.observe("k", "fs.read", "h1", same) is None
    first = d.observe("k", "fs.read", "h1", same)
    assert first is not None and first.kind == "repeat" and not first.stop
    denied = ToolResult.denied("scope", "outside allowed_files")
    sigs = [d.observe("k", "fs.write", f"h{i}", denied) for i in range(5)]  # args khác nhau, vẫn bị từ chối
    assert sigs[:4] == [None] * 4 and sigs[4] is not None and sigs[4].kind == "denials" and sigs[4].stop
    assert d.stopped("k") and not d.stopped("other")
    d.reset("k")
    assert not d.stopped("k")


def test_api_loop_ends_with_stuck_after_second_detection(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    rt, svc = env
    mock = MockProvider(echo=False)
    call = {"name": "fs_read", "input": {"path": "missing_module.py"}}
    mock.add_script("dev", [ScriptStep(text="đọc file", tool_calls=[call]) for _ in range(6)])  # type: ignore[list-item]
    assert svc.worktrees is not None
    info = svc.worktrees.ensure(ISSUE)
    outcome, _ = _run(rt, svc, "dev", Stage.IMPLEMENT, worktree=str(info.path), mock=mock)
    assert outcome.kind == "blocked" and (outcome.reason or "").startswith("STUCK")
    fourth = json.dumps(
        [b.content for m in mock.calls[3].messages for b in m.blocks if hasattr(b, "tool_use_id")]
    )
    assert "NOTE: You called fs.read 3 times" in fourth  # lần kẹt đầu: chỉ gợi ý, agent chạy tiếp
    stuck = [e for e in read_events(rt.layout, ISSUE) if e.event is EventType.STUCK_DETECTED]
    assert [e.data["stop"] for e in stuck] == [False, True]


def _tool_turn(i: int, size: int) -> list[Message]:
    use = Message(
        role="assistant", blocks=(ToolUseBlock(id=f"t{i}", name="fs_read", input={"path": f"f{i}"}),)
    )
    body = json.dumps({"status": "ok", "summary": f"read f{i}", "structured": {"content": "x" * size}})
    return [use, Message(role="user", blocks=(ToolResultBlock(tool_use_id=f"t{i}", content=body),))]


def test_condense_keeps_recent_turns_and_ids() -> None:
    msgs = [Message.user_text("brief")]
    for i in range(8):
        msgs += _tool_turn(i, 5000 if i else 100)
    out, n = condense_tool_results(msgs, keep_last_turns=6)
    assert n == 1 and len(out) == len(msgs)  # turn 0 nhỏ → giữ; turn 1 lớn và cũ → thu gọn; 6 turn cuối giữ
    results = [b for m in out for b in m.blocks if isinstance(b, ToolResultBlock)]
    assert [b.tool_use_id for b in results] == [f"t{i}" for i in range(8)]
    assert CONDENSED in results[1].content and "read f1" in results[1].content
    assert all(CONDENSED not in b.content for b in results[2:]) and isinstance(out[0].blocks[0], TextBlock)
    assert condense_tool_results(out, keep_last_turns=6)[1] == 0  # chạy lại không thu gọn lần nữa


def test_progress_reaches_next_attempt_and_blocked_report(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    rt, svc = env
    progress = {
        "items": [{"id": "AC1", "status": "done"}, {"id": "AC2", "status": "doing"}],
        "next": "make AC2 test pass",
    }
    rt.store.write(ISSUE, "progress.yaml", progress, actor=Actor(role="dev"))
    assert svc.agents and svc.brief_builder
    ref = resolve_stage_budget(rt.config.budgets, Stage.IMPLEMENT, Tier.T1)
    assert ref is not None
    brief = svc.brief_builder.build(
        svc.agents.get("dev"),
        BriefInputs(issue_id=ISSUE, stage=Stage.IMPLEMENT, tier=Tier.T1, budget=ref, rework=1),
    )
    assert "### progress.yaml" in brief.context and "make AC2 test pass" in brief.context
    report = build_blocked_report(
        issue=ISSUE,
        failure_class="budget_issue",
        from_stage=Stage.IMPLEMENT,
        reason="hết budget",
        hint=None,
        facts={},
        rework=1,
        reloc=0,
        branch="ddt/x",
        worktree="wt",
        files_touched=["calc.py"],
        spec_version=1,
        reopens=0,
        max_reopens=2,
        at="2026-10-11T00:00:00+00:00",
        progress=progress,
    )
    assert report["partial"]["progress"]["next"] == "make AC2 test pass"
    rt.registry.raise_if_invalid("blocked_report", report)
    learner = svc.agents.get("learner")
    assert svc.agent_runtime._multi_turn(learner)  # type: ignore[union-attr]  # Learner ghi memory.* → cần loop cả khi đi CLI

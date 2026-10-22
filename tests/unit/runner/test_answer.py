"""ANSWER — issue loại câu hỏi đi TRIAGE → ANSWER → DONE, không chạy LOCALIZE…MERGE; câu trả lời có secret thì dừng."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_development_team.app import Runtime
from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.eventlog.reader import read_events
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.stages import StageRegistry
from digital_development_team.runner.stages.stub import stub_handlers
from tests.conftest import TESTDATA

ISSUE = "DDT-0003"
QUESTION = {
    "type": "question",
    "route": "fast",
    "question": "Vì sao mean() trả float cả khi đầu vào toàn số nguyên?",
}


def _run(rt: Runtime, overrides: Mapping[str, Mapping[str, Any]]) -> Any:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    reg = default_handlers(stub=True)
    reg = StageRegistry({**{s: reg.get(s) for s in reg.stages()}, **stub_handlers(overrides)})
    return Runner(rt.services(with_agents=False), handlers=reg, stub=True).run(ISSUE, cancel=CancelToken())


def _entered(rt: Runtime) -> list[str]:
    return [
        e.stage.value
        for e in read_events(rt.layout, ISSUE)
        if e.event is EventType.STAGE_ENTER and e.stage is not None
    ]


def test_question_goes_straight_to_answer_and_done(runtime: Runtime) -> None:
    rep = _run(runtime, {"intake": QUESTION})
    assert rep.status == "done"
    assert _entered(runtime) == ["INGEST", "TRIAGE", "ANSWER"] or _entered(runtime)[-1] == "ANSWER"
    assert "LOCALIZE" not in _entered(runtime) and "MERGE" not in _entered(runtime)
    ans = runtime.store.read(ISSUE, "answer.yaml")
    assert ans.ref.written_by == "dev" and ans.content["references"] == ["calc.py#mean"]
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.facts["answer"]["secret_hits"] == 0 and cp.facts["answer"]["references"] == 1
    assert (
        "chars" in cp.facts["answer"] and "answer" not in cp.facts["answer"]
    )  # không nhét cả đoạn văn vào checkpoint
    row = runtime.db.one("SELECT passed, reason FROM role_outcomes WHERE issue_id=? AND role='dev'", (ISSUE,))
    assert row is not None and row["passed"] == 1 and "trả lời" in row["reason"]


def test_answer_with_secret_is_blocked(runtime: Runtime) -> None:
    leaky = {"answer": "Dùng key AKIAABCDEFGHIJKLMNOP trong config để gọi API, mean() vẫn trả float."}
    rep = _run(runtime, {"intake": QUESTION, "answer": leaky})
    assert rep.status == "blocked" and rep.stage is Stage.BLOCKED
    blocked = [e for e in read_events(runtime.layout, ISSUE) if e.event is EventType.BLOCKED]
    assert blocked and "secret" in str(blocked[-1].data["reason"])


def test_handover_for_question_shows_answer(runtime: Runtime) -> None:
    from digital_development_team.reporting import build_handover

    follow = [{"title": "Thêm mean_int() trả số nguyên", "type": "change_request"}]
    _run(runtime, {"intake": QUESTION, "answer": {"follow_up": follow}})
    md = build_handover(runtime.layout, runtime.db, runtime.store, ISSUE, now=runtime.clock.now()).markdown
    assert "## Câu hỏi" in md and QUESTION["question"] in md and "## Trả lời" in md
    assert "[change_request] Thêm mean_int()" in md and "## Acceptance criteria" not in md


def test_answer_stage_exposes_role_for_transport_overhead() -> None:
    """Runner cộng transport_overhead theo `handler.role`; thiếu role → ANSWER qua CLI chỉ có trần API (1 phút) và bị cắt."""
    assert getattr(default_handlers(stub=False).get(Stage.ANSWER), "role", None) == "dev"

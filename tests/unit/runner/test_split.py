"""Phase 9: Planner tách issue quá lớn → SPLIT (0 LLM) tạo issue con theo thứ tự, issue phụ thuộc chờ mọi con."""

from __future__ import annotations

from typing import Any

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import Actor, CancelToken
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.eventlog.reader import read_events
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.memory.outcomes import read_standup
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.recovery import choose
from tests.conftest import TESTDATA

SUBTASKS = [
    {"title": "Parser", "description": "Parse the new input format into tokens", "files": ["calc/parse.py"]},
    {
        "title": "Evaluator",
        "description": "Evaluate parsed tokens using the new parser",
        "files": ["calc/ev.py"],
        "depends_on": [1],
    },
]


def _plan(rt: Runtime, issue: str, subtasks: list[dict[str, Any]]) -> None:
    plan = dict(rt.store.read(issue, "plan.yaml").content)
    rt.store.write(issue, "plan.yaml", {**plan, "subtasks": subtasks}, actor=Actor(role="planner"))


def test_split_creates_ordered_children_and_moves_dependents(runtime: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    q = Queue(runtime.db, runtime.clock)
    assert q.get("DDT-0002") is not None and q.get("DDT-0002").blocked_by == ("DDT-0001",)  # type: ignore[union-attr]
    runner = Runner(runtime.services(with_agents=False), handlers=default_handlers(stub=True), stub=True)
    assert runner.run("DDT-0001", cancel=CancelToken(), until=Stage.LOCALIZE).status == "running"
    _plan(runtime, "DDT-0001", SUBTASKS)
    rep = runner.run("DDT-0001", cancel=CancelToken())
    assert rep.status == "done"
    cp = runtime.checkpoints.load("DDT-0001")
    assert cp is not None
    children = cp.facts["split"]["children"]
    assert len(children) == 2 and cp.facts["split"]["dependents_moved"] == ["DDT-0002"]
    first, second = (runtime.store.read(c, "item.json").content for c in children)
    assert (
        "1/2: Parser" in first["title"]
        and first["raw"]["split_from"] == "DDT-0001"
        and "split" in first["labels"]
    )
    assert second["blocked_by"] == [children[0]] and "Files in scope: calc/ev.py" in second["body"]
    assert q.get("DDT-0002").blocked_by == tuple(children)  # type: ignore[union-attr]
    assert any(e.event is EventType.ISSUE_SPLIT for e in read_events(runtime.layout, "DDT-0001"))
    assert "tách thành" in read_standup(runtime.layout)[-1]
    # issue con không tách tiếp: subtasks bị bỏ qua, đi SPEC như thường
    child = children[0]
    assert runner.run(child, cancel=CancelToken(), until=Stage.LOCALIZE).status == "running"
    _plan(runtime, child, SUBTASKS)
    assert runner.run(child, cancel=CancelToken(), until=Stage.SPEC).status == "running"
    stages = [e.stage for e in read_events(runtime.layout, child) if e.event is EventType.STAGE_ENTER]
    assert Stage.SPLIT not in stages and Stage.SPEC in stages
    # chạy lại SPLIT trên cha không tạo trùng
    before = len(q.list())
    from digital_development_team.runner.stages.split import SplitStage

    assert (
        SplitStage().run(runner.load_state("DDT-0001"), _ctx(runner, runtime)).facts["split"]["created"] == 2
    )
    assert len(q.list()) == before


def _ctx(runner: Runner, rt: Runtime) -> Any:
    from digital_development_team.gates.service import GateService
    from digital_development_team.runner.context import StageContext

    svc = runner.services
    return StageContext(
        svc=svc,
        config=svc.config,
        state=runner.load_state("DDT-0001"),
        cancel=CancelToken(),
        clock=rt.clock,
        gates=GateService(svc.db, rt.clock),
        budget=None,
    )


def test_decompose_rung_sends_too_large_back_to_planner() -> None:
    cfg = load_config().policy.recovery
    plan = choose(cfg, failure_class="too_large", from_stage=Stage.IMPLEMENT, counts={}, model_roles=[])
    assert (
        choose(cfg, failure_class="budget_stage", from_stage=Stage.TRIAGE, counts={}, model_roles=[]) is None
    )
    assert plan is not None and plan.rung == "decompose" and plan.to is Stage.LOCALIZE
    assert (
        choose(
            cfg,
            failure_class="too_large",
            from_stage=Stage.IMPLEMENT,
            counts={"decompose": 1},
            model_roles=[],
        )
        is None
    )

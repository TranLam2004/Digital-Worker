"""Stage hết wall-clock → circuit breaker + BLOCKED. Trước đây runner coi là pause (giống người bấm STOP), mỗi lần
chạy lại được hạn mới nên issue lặp vô hạn và event log ghi nhầm là stop_requested."""

from __future__ import annotations

from dataclasses import replace

from digital_development_team.app import Runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken
from digital_development_team.core.enums import EventType, IssueStatus, Stage
from digital_development_team.core.models import StageResult
from digital_development_team.eventlog.reader import read_events
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.state import IssueState
from tests.conftest import TESTDATA

ISSUE = "DDT-0001"


class _Slow:
    """Stage chạy mãi, chỉ dừng khi token bị huỷ (như AgentLoop kiểm cancel giữa các lượt)."""

    stage = Stage.TRIAGE
    uses_llm = False

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        while True:
            if ctx.cancel.wait(0.02):
                ctx.cancel.raise_if_cancelled()


def test_stage_deadline_blocks_instead_of_pausing(runtime: Runtime) -> None:
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    rt = replace(
        runtime, config=load_config(overrides=[("budgets", "stages.TRIAGE.T1.wall", 0.003)])
    )  # ~0.2s
    reg = default_handlers(stub=True).with_(TRIAGE=_Slow())
    rep = Runner(rt.services(with_agents=False), handlers=reg, stub=True).run(ISSUE, cancel=CancelToken())
    assert rep.status == "blocked" and "hết thời gian" in str(rep.reason)
    cp = runtime.checkpoints.load(ISSUE)
    assert cp is not None and cp.status is IssueStatus.BLOCKED
    evs = read_events(runtime.layout, ISSUE)
    breakers = [e for e in evs if e.event is EventType.CIRCUIT_BREAKER]
    assert breakers and breakers[-1].data["axis"] == "wall" and breakers[-1].data["stage"] == "TRIAGE"
    assert not any(e.event is EventType.STOP_REQUESTED for e in evs)  # không giả làm người bấm STOP


def test_parent_cancel_still_pauses(runtime: Runtime) -> None:
    """Người bấm STOP / Ctrl-C (token cha bị huỷ) vẫn là pause, resume được."""
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    cancel = CancelToken()
    cancel.cancel("STOP (human:P2)")
    reg = default_handlers(stub=True).with_(TRIAGE=_Slow())
    rep = Runner(runtime.services(with_agents=False), handlers=reg, stub=True).run(ISSUE, cancel=cancel)
    assert rep.status == "paused"

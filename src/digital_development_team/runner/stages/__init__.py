"""Stage handlers. Mỗi handler trả StageResult(facts) — KHÔNG tự chuyển stage.

default_handlers(stub=True): INGEST thật + mọi stage agent/máy bằng stub (artifact hợp lệ, 0 LLM)
default_handlers(stub=False): stage agent thật được thêm theo bước build (B9 IMPLEMENT, B10 TRIAGE/LOCALIZE/SPEC, B11 …).
"""

from __future__ import annotations

from collections.abc import Mapping

from digital_development_team.core.enums import GateId, Stage
from digital_development_team.core.errors import ConfigError
from digital_development_team.runner.stages.agent_stage import AgentStage
from digital_development_team.runner.stages.answer import AnswerStage
from digital_development_team.runner.stages.base import StageHandler
from digital_development_team.runner.stages.canary import CanaryVerify
from digital_development_team.runner.stages.contest import ContestIntake
from digital_development_team.runner.stages.gate import GateStage
from digital_development_team.runner.stages.gate_wait import GateWaitStage
from digital_development_team.runner.stages.ingest import IngestStage
from digital_development_team.runner.stages.intake_check import IntakeConsistency
from digital_development_team.runner.stages.machine_gate import MachineGateStage
from digital_development_team.runner.stages.merge import MergeStage
from digital_development_team.runner.stages.preflight import ImplementPreflight
from digital_development_team.runner.stages.split import SplitStage
from digital_development_team.runner.stages.stub import stub_handlers
from digital_development_team.runner.stages.verify_fallback import VerifyFallback


class StageRegistry:
    def __init__(self, handlers: Mapping[Stage, StageHandler]) -> None:
        self._h = dict(handlers)

    def get(self, stage: Stage) -> StageHandler:
        h = self._h.get(stage)
        if h is None:
            raise ConfigError(
                f"stage {stage.value} chưa có handler",
                hint="dùng --stub-stages hoặc chờ bước build tương ứng",
            )
        return h

    def has(self, stage: Stage) -> bool:
        return stage in self._h

    def with_(self, **overrides: StageHandler) -> StageRegistry:
        merged = dict(self._h)
        for name, h in overrides.items():
            merged[Stage(name)] = h
        return StageRegistry(merged)

    def stages(self) -> list[Stage]:
        return sorted(self._h, key=lambda s: list(Stage).index(s))


def default_handlers(*, stub: bool) -> StageRegistry:
    base: dict[Stage, StageHandler] = {
        Stage.INGEST: IngestStage(),
        Stage.G0_WAIT: GateWaitStage(Stage.G0_WAIT, GateId.G0),
        Stage.G1_WAIT: GateWaitStage(Stage.G1_WAIT, GateId.G1),
        Stage.SPLIT: SplitStage(),  # 0 LLM: plan.subtasks → issue con
    }
    if stub:
        base.update(stub_handlers())
    else:
        # tắt mặc định · item thi đấu: loại CR/BUG/PI theo đề, không nhánh chờ người
        base[Stage.TRIAGE] = ContestIntake(IntakeConsistency(AgentStage(Stage.TRIAGE, "intake")))
        base[Stage.LOCALIZE] = AgentStage(Stage.LOCALIZE, "planner")
        base[Stage.SPEC] = AgentStage(Stage.SPEC, "analyst")
        base[Stage.IMPLEMENT] = ImplementPreflight(
            AgentStage(Stage.IMPLEMENT, "dev")
        )  # kiểm lệnh spec + baseline
        base[Stage.MACHINE_GATE] = MachineGateStage()
        # ~1/5 task T1/T2 có lỗi tiêm · Verifier blocked 2 lần → evidence inconclusive do harness ghi
        base[Stage.VERIFY] = VerifyFallback(CanaryVerify(AgentStage(Stage.VERIFY, "verifier")))
        base[Stage.REVIEW] = AgentStage(Stage.REVIEW, "security")
        base[Stage.GATE] = GateStage(AgentStage(Stage.GATE, "leader"))
        base[Stage.MERGE] = MergeStage()
        base[Stage.ANSWER] = AnswerStage(AgentStage(Stage.ANSWER, "dev"))
    return StageRegistry(base)


__all__ = ["StageHandler", "StageRegistry", "default_handlers"]

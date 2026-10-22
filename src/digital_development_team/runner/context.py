"""Services (hạ tầng runner cần) + StageContext (những gì một StageHandler được phép chạm vào).

runner (L7) không import app (L8): app.services(rt) lắp Services từ Runtime.
"""

from __future__ import annotations

from dataclasses import dataclass

from digital_development_team.agents.brief import BriefBuilder
from digital_development_team.agents.spec import AgentRegistry
from digital_development_team.blackboard.checkpoints import CheckpointStore
from digital_development_team.blackboard.store import FileStore
from digital_development_team.budget.meter import BudgetMeter
from digital_development_team.checklist.loader import ChecklistRegistry
from digital_development_team.config.loader import ResolvedConfig
from digital_development_team.config.models import SandboxCfg
from digital_development_team.core.context import Actor, CancelToken, Clock
from digital_development_team.core.protocols import AgentRuntime, EventSink, Sandbox
from digital_development_team.gamemaster.port import GameMaster
from digital_development_team.gates.service import GateService
from digital_development_team.isolation.worktree import WorktreeManager
from digital_development_team.llm.registry import ModelRegistry
from digital_development_team.runner.state import IssueState
from digital_development_team.schema.registry import SchemaRegistry
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.harness_fields import HarnessFields

RUNNER_ACTOR = Actor(role="runner")


@dataclass(frozen=True, slots=True)
class Services:
    layout: HomeLayout
    db: Database
    store: FileStore
    checkpoints: CheckpointStore
    sink: EventSink
    schemas: SchemaRegistry
    models: ModelRegistry
    clock: Clock
    config: ResolvedConfig
    # agent runtime (None → chỉ stub stages chạy được)
    agents: AgentRegistry | None = None
    brief_builder: BriefBuilder | None = None
    agent_runtime: AgentRuntime | None = None
    worktrees: WorktreeManager | None = None
    sandbox: Sandbox | None = None
    sandbox_cfg: SandboxCfg | None = None
    artifacts: ArtifactStore | None = None
    checklists: ChecklistRegistry | None = None
    harness_fields: HarnessFields | None = None
    gamemaster: GameMaster | None = None  # thi đấu: start working · submit (None = không có trận)


@dataclass(frozen=True, slots=True)
class StageContext:
    svc: Services
    config: ResolvedConfig
    state: IssueState
    cancel: CancelToken
    clock: Clock
    gates: GateService
    budget: BudgetMeter | None  # None = stage 0 LLM
    stub: bool = False

    @property
    def issue_id(self) -> str:
        return self.state.issue_id

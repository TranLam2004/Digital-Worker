"""Runner — trái tim. Không gọi LLM. Quyết stage nào chạy, checkpoint, transition theo policy.yaml.

    report = Runner(rt, handlers=default_handlers(stub=True)).run("DDT-0001", cancel=CancelToken())
    report.status  # done · blocked · paused · waiting_gate · running(--until)
    report.exit_code  # 0 done · 1 blocked · 3 paused · 4 waiting gate

Vòng lặp: STOP? → config snapshot → phase ready: stage.run → checkpoint(stage_done)
          → transitions.next(scope) → checkpoint(ready) → emit. Crash giữa chừng → resume đúng phase.
"""

from __future__ import annotations

from digital_development_team.runner.runner import Runner, RunReport
from digital_development_team.runner.stages import StageRegistry, default_handlers
from digital_development_team.runner.transitions import Decision, TransitionTable

__all__ = ["Decision", "RunReport", "Runner", "StageRegistry", "TransitionTable", "default_handlers"]

"""Gates — 4 điểm dừng chờ người (70-human-control/02-gates.md).

G0 Scope · G1 Merge: GateService trên bảng gate_requests (CLI/web ghi, runner poll + emit).
G2 Destructive: TODO(P2-#16) policy lớp 4 → user.ask.
G3 Kill: StopSwitch = file state/STOP.
"""

from __future__ import annotations

from digital_development_team.gates.decisions import (
    REJECT_REASONS,
    GateError,
    batch_approve,
    check_choice,
    pick_choice,
    resolve_gate,
)
from digital_development_team.gates.service import GateService, OpenGate
from digital_development_team.gates.stop import StopSwitch

__all__ = [
    "REJECT_REASONS",
    "GateError",
    "GateService",
    "OpenGate",
    "StopSwitch",
    "batch_approve",
    "check_choice",
    "pick_choice",
    "resolve_gate",
]

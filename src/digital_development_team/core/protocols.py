"""Các Protocol chính. Nhỏ, 1–3 method. Conformance kiểm bằng mypy trong tests/contract/test_protocols.py.

Không dùng runtime_checkable — nó chỉ kiểm tên method.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from digital_development_team.core.context import Actor, CancelToken, RunContext, ToolContext
from digital_development_team.core.enums import GateId, OutputMode, PermissionMode, Stage
from digital_development_team.core.events import Event, EventDraft
from digital_development_team.core.ids import IssueId, SlotName
from digital_development_team.core.llm import ChatRequest, ChatResponse, ProviderCaps, ToolSpec
from digital_development_team.core.models import (
    AgentOutcome,
    Artifact,
    ArtifactRef,
    Brief,
    ExecResult,
    GateDecision,
    GateRequest,
    GateTicket,
    PatchBundle,
    SandboxProfile,
    StageResult,
    SubagentRequest,
    SubagentResult,
    ToolResult,
)


class LLMProvider(Protocol):
    """Adapter một provider. Raise RateLimited | ProviderUnavailable | ContextOverflow | Cancelled.
    Refusal KHÔNG raise — trả StopReason.REFUSAL."""

    @property
    def name(self) -> str: ...

    @property
    def caps(self) -> ProviderCaps: ...

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse: ...


class Tool(Protocol):
    """Không tự check policy (dispatcher làm). Lỗi nghiệp vụ → ToolResult(status='error')."""

    @property
    def spec(self) -> ToolSpec: ...

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult: ...


class Sandbox(Protocol):
    def run(
        self,
        cmd: str,
        *,
        cwd: Path,
        profile: SandboxProfile,
        timeout_s: float,
        stdin: str | None = None,
        cancel: CancelToken,
    ) -> ExecResult: ...


class Store(Protocol):
    """Blackboard. write: kiểm owner → validate schema → version → index → emit."""

    def read(self, issue: IssueId, slot: SlotName, *, version: int | None = None) -> Artifact: ...

    def write(
        self,
        issue: IssueId,
        slot: SlotName,
        content: Mapping[str, Any] | PatchBundle,
        *,
        actor: Actor,
    ) -> ArtifactRef: ...

    def latest(self, issue: IssueId, slot: SlotName) -> ArtifactRef | None: ...

    def list(self, issue: IssueId) -> Sequence[ArtifactRef]: ...


class EventSink(Protocol):
    def emit(self, draft: EventDraft) -> Event: ...


class Gate(Protocol):
    @property
    def id(self) -> GateId: ...

    def open(self, req: GateRequest) -> GateTicket: ...

    def poll(self, ticket: GateTicket, *, now: datetime) -> GateDecision | None: ...

    def resolve(self, ticket_id: str, decision: GateDecision) -> None: ...


class AgentSpecLike(Protocol):
    """Protocol thuộc tính tối thiểu để core không import agents."""

    @property
    def name(self) -> str: ...

    @property
    def tools(self) -> Sequence[str]: ...

    @property
    def reads(self) -> Sequence[str]: ...

    @property
    def writes(self) -> Sequence[str]: ...

    @property
    def permission_mode(self) -> PermissionMode: ...

    @property
    def output_mode(self) -> OutputMode: ...


class AgentRuntime(Protocol):
    def run(self, spec: AgentSpecLike, brief: Brief, ctx: RunContext) -> AgentOutcome: ...


class IssueStateLike(Protocol):
    @property
    def issue_id(self) -> str: ...

    @property
    def stage(self) -> Stage: ...


class StageHandler(Protocol):
    """KHÔNG tự chuyển stage — chỉ trả facts cho TransitionTable."""

    @property
    def stage(self) -> Stage: ...

    def run(self, state: Any, ctx: Any) -> StageResult: ...


class SubagentSpawner(Protocol):
    """Cắt vòng tools → agents. agents.delegation hiện thực, app inject vào tools.agent."""

    def spawn(self, req: SubagentRequest, parent: RunContext) -> str: ...

    def collect(self, ids: Sequence[str] | None, timeout_s: float) -> Sequence[SubagentResult]: ...

    def stop(self, sub_id: str, reason: str) -> None: ...

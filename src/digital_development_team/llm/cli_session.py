"""Kiểu dùng chung cho mọi transport CLI (Claude Code `claude -p`, Codex `codex exec`).

CliSession  file JSON harness ghi, process `ddt mcp --session` đọc để lắp đúng tool/policy cho agent.
TurnInfo    một lượt (hoặc một tool call) báo về harness → budget + transcript + kill khi chạm trần.
CliAgentResult  kết quả một lần chạy agent loop trong CLI.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from digital_development_team.core.llm import StopReason, Usage
from digital_development_team.util.fsio import ENCODING, write_text_atomic


@dataclass(frozen=True, slots=True)
class CliSession:
    home: str
    repo: str | None
    config_dir: str | None
    issue_id: str
    role: str
    stage: str
    tier: str
    worktree: str | None
    allowed_files: tuple[str, ...]
    spec_values: dict[str, str | None]
    autonomy: str
    tools: tuple[str, ...]  # canonical, theo frontmatter
    permission_tool: bool = True  # tool `policy` cho --permission-prompt-tool (chỉ Claude Code cần)
    preset: str | None = None  # preset/flags của worker → MCP con dựng runtime y hệt (vd codex_all, --match)
    flags: tuple[str, ...] = ()

    def dump(self, path: Path) -> Path:
        write_text_atomic(path, json.dumps(asdict(self), ensure_ascii=False, indent=1), fsync=False)
        return path

    @classmethod
    def load(cls, path: Path) -> CliSession:
        d = json.loads(path.read_text(encoding=ENCODING))
        d["allowed_files"] = tuple(d.get("allowed_files", ()))
        d["tools"] = tuple(d.get("tools", ()))
        d["flags"] = tuple(d.get("flags", ()))
        return cls(**d)


@dataclass(frozen=True, slots=True)
class TurnInfo:
    index: int
    text: str
    tool_calls: tuple[str, ...]  # wire name
    usage: Usage
    stop_reason: str | None
    model: str


@dataclass
class CliAgentResult:
    final_text: str = ""
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    is_error: bool = False
    subtype: str | None = None
    rate_limited: bool = False
    killed: bool = False
    exit_code: int = 0
    permission_denials: list[Any] = field(default_factory=list)
    stderr_tail: str = ""
    model: str = ""

    @property
    def stop(self) -> StopReason:
        if self.killed:
            return StopReason.OTHER
        return StopReason.END_TURN if not self.is_error else StopReason.OTHER

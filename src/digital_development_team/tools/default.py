"""default_tools — lắp ToolRegistry đầy đủ cho một runtime (fs · blackboard · git · exec · test · memory … + stubs)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from digital_development_team.blackboard.store import FileStore
from digital_development_team.config.models import NetworkCfg, SandboxCfg
from digital_development_team.core.context import ToolContext
from digital_development_team.core.errors import StoreError
from digital_development_team.core.protocols import Sandbox
from digital_development_team.isolation.git import Git
from digital_development_team.memory.skill_scan import ScanContext
from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.base import ToolRegistry
from digital_development_team.tools.blackboard import BlackboardTools
from digital_development_team.tools.catalog import ToolCatalog
from digital_development_team.tools.codegraph import CodeGraphTools
from digital_development_team.tools.coverage import BASELINE_FILE
from digital_development_team.tools.docs import DocsTools
from digital_development_team.tools.exec import ExecTools
from digital_development_team.tools.fs import FsTools
from digital_development_team.tools.git import GitTools
from digital_development_team.tools.harness_fields import HarnessFields
from digital_development_team.tools.llm_ask import AskRoleFn, LlmAskTools
from digital_development_team.tools.mcp_client import McpPool
from digital_development_team.tools.memory import MemoryTools, MemoryWiring
from digital_development_team.tools.pkg import PkgTools
from digital_development_team.tools.security import SecurityTools
from digital_development_team.tools.skill import SkillLookup, SkillTools
from digital_development_team.tools.stubs import register_stubs
from digital_development_team.tools.test import TestTools
from digital_development_team.tools.web import SearchBackend, WebTools


def spec_reader_for(store: FileStore) -> Callable[[str], Mapping[str, Any] | None]:
    def _read(issue: str) -> Mapping[str, Any] | None:
        try:
            return store.read(issue, "spec.yaml").content
        except StoreError:
            return None

    return _read


def default_tools(
    catalog: ToolCatalog,
    *,
    store: FileStore,
    artifacts: ArtifactStore,
    sandbox: Sandbox,
    sandbox_cfg: SandboxCfg,
    git: Git | None,
    base_ref: Callable[[ToolContext], str] | None = None,
    rg: str | None = None,
    harness_fields: HarnessFields | None = None,
    memory_dir: Path | None = None,
    impact_thresholds: Mapping[str, int] | None = None,
    network: NetworkCfg | None = None,
    web_cache: Path | None = None,
    ask_role: AskRoleFn | None = None,
    search_backends: Sequence[SearchBackend] = (),
    skills: SkillLookup | None = None,
    memory: MemoryWiring | None = None,
    mcp: McpPool | None = None,
) -> tuple[ToolRegistry, BlackboardTools]:
    reg = ToolRegistry(catalog)
    reg.register_namespace("fs", FsTools(artifacts, rg=rg).handlers())
    bb = BlackboardTools(store, git=git, base_ref=base_ref, harness_fields=harness_fields)
    reg.register_namespace("blackboard", bb.handlers())
    if git is not None:
        reg.register_namespace("git", GitTools(git, base_ref=base_ref).handlers())
    reg.register_namespace("exec", ExecTools(sandbox, sandbox_cfg, git=git).handlers())
    reg.register_namespace(
        "test",
        TestTools(
            sandbox,
            sandbox_cfg,
            artifacts,
            spec_reader=spec_reader_for(store),
            git=git,
            base_ref=base_ref,
            baseline_path=(memory_dir / BASELINE_FILE) if memory_dir else None,
        ).handlers(),
    )
    reg.register_namespace(
        "security", SecurityTools(sandbox, sandbox_cfg, artifacts, git=git, base_ref=base_ref).handlers()
    )
    if git is not None:  # index ở repo chính; không repo → stub NOT_IMPLEMENTED
        reg.register_namespace(
            "codegraph",
            CodeGraphTools(
                sandbox, sandbox_cfg, git.root, git=git, base_ref=base_ref, thresholds=impact_thresholds
            ).handlers(),
        )
    if network is not None:  # tra cứu khi KB/repo không có: allowlist + cache + model rẻ đọc trang
        ask = (lambda system, prompt, n: ask_role("llm_ask", system, prompt, n)) if ask_role else None
        web = WebTools(network, cache_dir=web_cache, ask=ask, backends=search_backends)
        reg.register_namespace("web", web.handlers())
        reg.register_namespace(
            "docs", DocsTools(web, sandbox=sandbox, sandbox_cfg=sandbox_cfg, ask=ask).handlers()
        )
        reg.register_namespace("pkg", PkgTools(sandbox, sandbox_cfg, network).handlers())
    if ask_role is not None:
        reg.register_namespace("llm", LlmAskTools(ask_role).handlers())
    if skills is not None:
        reg.register_namespace("skill", SkillTools(skills).handlers())
    if memory is not None:
        scan = ScanContext(
            tool_names=frozenset(catalog.names()),
            command_guard=memory.command_guard,
            network=network or NetworkCfg(),
            max_tokens=memory.cfg.skill_max_tokens,
        )
        reg.register_namespace("memory", MemoryTools(memory.db, memory.layout, memory.cfg, scan).handlers())
    if mcp is not None:  # MCP server ngoài: cùng Dispatcher/policy/event như tool nội bộ
        reg.register_namespace("mcp", mcp.handlers())
    register_stubs(reg, catalog)
    return reg, bb


def git_for(repo: Path | None, scratch: Path) -> Git | None:
    return Git(repo, scratch=scratch) if repo else None

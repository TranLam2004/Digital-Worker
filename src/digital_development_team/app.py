"""Composition root — nơi DUY NHẤT nối các package thành runtime. CLI/web/test gọi build_runtime().

Thêm thành phần theo bước build: B4 llm registry · B5 runner · B8 sandbox/policy/tools · B9 isolation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from digital_development_team.agents.skills import SkillIndex, SkillRegistry
from digital_development_team.blackboard.checkpoints import CheckpointStore
from digital_development_team.blackboard.store import FileStore
from digital_development_team.config.loader import ResolvedConfig, load_config
from digital_development_team.config.resources import AssetResolver, build_resolver
from digital_development_team.core.context import CancelToken, Clock, SystemClock
from digital_development_team.core.errors import ConfigError
from digital_development_team.eventlog.writer import JsonlEventSink
from digital_development_team.llm.mock import MockProvider
from digital_development_team.llm.registry import ModelRegistry
from digital_development_team.runner.context import Services
from digital_development_team.schema.registry import SchemaRegistry
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.tools.catalog import ToolCatalog

log = logging.getLogger(__name__)

PROVIDER_PRESETS: dict[str, str | None] = {
    "mock": "mock_all",
    "anthropic": None,
    "openai": "all_openai",
    "local": "local_dev",
    "cli": "cli_all",
    "codex": "codex_all",
}
"""--provider X → preset models.yaml tương ứng. mock còn ép registry về mock cho mọi alias."""


@dataclass(frozen=True, slots=True)
class Runtime:
    home: Path
    layout: HomeLayout
    resolver: AssetResolver
    config: ResolvedConfig
    registry: SchemaRegistry
    db: Database
    sink: JsonlEventSink
    store: FileStore
    checkpoints: CheckpointStore
    models: ModelRegistry
    clock: Clock
    repo: Path | None = None
    config_dir: Path | None = None  # cho process con (ddt mcp) dựng lại runtime cùng config

    def close(self) -> None:
        self.db.close()

    def reloaded(self, config: ResolvedConfig) -> Runtime:
        """Runtime cùng db/sink/store nhưng đọc lại lớp override và config mới. Worker gọi giữa hai issue khi
        JD · skill · checklist · tool · config đổi (override trong <home>/overrides, `ddt runtime set`) để khỏi phải khởi động lại."""
        return replace(
            self,
            resolver=build_resolver(home=self.home, config_dir=self.config_dir),
            config=config,
            models=self.models.with_config(config.models),
        )

    def services(self, *, with_agents: bool = True) -> Services:
        """Gói hạ tầng cho Runner (runner L7 không import app). with_agents → tools + policy + AgentLoop + worktree.

        Registry JD luôn có (rẻ, đọc lười) để runner ghi đúng phiên bản JD vào số liệu học hỏi kể cả khi chạy stub.
        """
        from digital_development_team.agents import AgentRegistry

        agents = AgentRegistry(self.resolver)
        agents.grant(
            mcp_grants(self)
        )  # tool MCP server ngoài đã ghim → role khai trong policy.yaml#mcp_client
        agents.revoke(self.config.policy.tool_denylist)
        base = Services(
            layout=self.layout,
            db=self.db,
            store=self.store,
            checkpoints=self.checkpoints,
            sink=self.sink,
            schemas=self.registry,
            models=self.models,
            clock=self.clock,
            config=self.config,
            agents=agents,
        )
        if not with_agents:
            return base
        from digital_development_team.agents import (
            BriefBuilder,
            FsRepoContext,
            NullRepoContext,
            SkillRegistry,
        )
        from digital_development_team.agents.loop import AgentLoop
        from digital_development_team.agents.repo_context import RepoContext
        from digital_development_team.isolation.worktree import WorktreeManager
        from digital_development_team.memory import NullMemory
        from digital_development_team.memory.knowledge import KnowledgeMemory

        worktrees: WorktreeManager | None = None

        def base_ref(tctx: Any) -> str:
            cp = self.checkpoints.load(tctx.issue_id)
            if cp and cp.base_commit:
                return cp.base_commit
            return worktrees.default_branch() if worktrees else "main"

        tooling = build_tooling(self, base_ref=base_ref)
        if tooling.git is not None:
            worktrees = WorktreeManager(tooling.git, self.layout)

        def repo_for(issue: str) -> RepoContext:
            if worktrees is not None and worktrees.exists(issue):
                return FsRepoContext(worktrees.path(issue), repo_md_path=self.layout.memory_dir / "REPO.md")
            if self.repo is not None:
                return FsRepoContext(self.repo, repo_md_path=self.layout.memory_dir / "REPO.md")
            return NullRepoContext()

        skills = SkillRegistry(self.resolver)
        brief_builder = BriefBuilder(
            store=self.store,
            schemas=self.registry,
            skills=skills,
            resolver=self.resolver,
            policy=self.config.policy,
            sensitive_globs=list(self.config.sensitive_paths.globs()),
            repo_provider=repo_for,
            # fact · lesson · skill học được → brief; --no-learn tắt để đo memory_lift
            memory=NullMemory()
            if self.config.policy.no_learn
            else KnowledgeMemory(self.layout, self.db, self.config.policy.memory),
        )
        loop = AgentLoop(
            models=self.models,
            catalog=tooling.catalog,
            dispatcher=tooling.dispatcher,
            store=self.store,
            sink=self.sink,
            resolver=self.resolver,
            agents=agents,
            skills=skills,
            budgets=self.config.budgets,
            policy=self.config.policy,
            home=self.home,
            repo=self.repo,
            config_dir=self.config_dir,
            preset=self.config.preset,
            flags=self.config.flags,
        )
        from digital_development_team.checklist import ChecklistRegistry

        return replace(
            base,
            agents=agents,
            brief_builder=brief_builder,
            agent_runtime=loop,
            worktrees=worktrees,
            sandbox=tooling.sandbox,
            sandbox_cfg=tooling.sandbox_cfg,
            artifacts=tooling.artifacts,
            checklists=ChecklistRegistry(self.resolver),
            harness_fields=tooling.harness_fields,
        )


def build_runtime(
    home: Path,
    *,
    config_dir: Path | None = None,
    flags: Sequence[str] = (),
    preset: str | None = None,
    provider: str | None = None,
    golden_dir: Path | None = None,
    clock: Clock | None = None,
    validate_events: bool = True,
    repo: Path | None = None,
) -> Runtime:
    clock = clock or SystemClock()
    layout = HomeLayout(home).ensure()
    resolver = build_resolver(home=home, config_dir=config_dir)
    if provider is not None and provider not in PROVIDER_PRESETS:
        raise ConfigError(f"--provider {provider!r} lạ", hint=f"một trong {sorted(PROVIDER_PRESETS)}")
    preset = preset or (PROVIDER_PRESETS.get(provider) if provider else None)
    config = load_config(home=home, config_dir=config_dir, flags=flags, preset=preset, resolver=resolver)
    registry = SchemaRegistry(resolver)
    db = Database(layout.db_path)
    validate = (lambda d: registry.raise_if_invalid("event", d)) if validate_events else None
    sink = JsonlEventSink(layout, db, clock=clock, validate=validate)
    store = FileStore(layout, db, registry, sink, clock=clock)
    checkpoints = CheckpointStore(layout, db)
    mock = MockProvider(echo=golden_dir is None)
    if golden_dir is not None:
        mock.load_dir(golden_dir)
    models = ModelRegistry(
        config.models,
        provider_override="mock" if provider == "mock" else None,
        clock=clock,
        providers={"mock": mock},
        scratch=layout.state_dir / "tmp",
    )
    log.debug("runtime: home=%s config=%s", home, config.fingerprint[:8])
    return Runtime(
        home=home,
        layout=layout,
        resolver=resolver,
        config=config,
        registry=registry,
        db=db,
        sink=sink,
        store=store,
        checkpoints=checkpoints,
        models=models,
        clock=clock,
        repo=repo.resolve() if repo else None,
        config_dir=config_dir,
    )


@dataclass(frozen=True, slots=True)
class Tooling:
    """Tool stack cho một runtime: catalog · policy · sandbox · dispatcher · blackboard tool (đếm attempts)."""

    catalog: ToolCatalog
    dispatcher: Any
    blackboard: Any
    policy: Any
    sandbox: Any
    artifacts: Any
    git: Any
    sandbox_cfg: Any
    harness_fields: Any


def _ask_role(rt: Runtime) -> Callable[[str, str, str, int], str]:
    """llm.ask / web.fetch(question) / docs.lookup(question): một call không tool qua ModelRegistry (tính cost như
    mọi call). Role thiếu trong models.yaml → llm_ask."""
    from digital_development_team.core.llm import ChatRequest, Message

    def ask(role: str, system: str, prompt: str, max_tokens: int) -> str:
        try:
            client = rt.models.client(role)
        except ConfigError:
            client = rt.models.client("llm_ask")
        req = ChatRequest(
            model=client.ref,
            system=system,
            messages=(Message.user_text(prompt),),
            max_tokens=max_tokens,
            metadata={"role": role},
        )
        return client.chat(req, cancel=CancelToken()).response.text

    return ask


def _search_backends(rt: Runtime) -> list[Any]:
    """Backend web.search theo policy.network.search_backends. Model đang là mock (test/dry-run) → không backend thật."""
    from digital_development_team.llm.claude_cli_provider import ClaudeCliProvider
    from digital_development_team.tools.web import SearchBackend, anthropic_backend, brave_backend

    models = rt.config.models
    haiku = models.aliases.get("haiku")
    if haiku is None or haiku.provider == "mock":
        return []
    net = rt.config.policy.network
    out: list[Any] = []
    for name in net.search_backends:
        if name == "claude_cli":
            pc = models.providers.get("anthropic_cli")
            if pc is not None and ClaudeCliProvider.binary_available(pc):
                prov = ClaudeCliProvider(pc, scratch=rt.layout.state_dir / "tmp")

                def _cli(q: str, n: int, prefer: Any, cancel: Any, _p: ClaudeCliProvider = prov) -> Any:
                    return _p.web_search(q, model="haiku", max_results=n, prefer_hosts=prefer, cancel=cancel)

                out.append(SearchBackend("claude_cli", _cli))
        elif name == "anthropic_api":
            model = haiku.model if haiku.provider == "anthropic" else "claude-haiku-4-5-20251001"
            b = anthropic_backend(model, net)
            if b is not None:
                out.append(b)
        elif name == "brave":
            b = brave_backend(net)
            if b is not None:
                out.append(b)
    return out


def mcp_grants(rt: Runtime) -> dict[str, list[str]]:
    """role → tool MCP được cấp (chỉ server đã sync + pin). Dùng chung cho runtime và `ddt mcp` (transport CLI)."""
    from digital_development_team.tools.mcp_client import role_grants, snapshot_ops

    cfg = rt.config.policy.mcp_client
    if not cfg.servers:
        return {}
    return role_grants(cfg, snapshot_ops(cfg, rt.layout.state_dir)[0])


def build_tooling(
    rt: Runtime, *, base_ref: Callable[[Any], str] | None = None, repo: Path | None = None
) -> Tooling:
    """Lắp tools + policy + sandbox. repo: repo đích (git) — None → không có git.* / patch/."""
    from digital_development_team.policy.engine import PolicyEngine
    from digital_development_team.sandbox import build_sandbox
    from digital_development_team.tools.artifacts import ArtifactStore
    from digital_development_team.tools.default import default_tools, git_for
    from digital_development_team.tools.dispatch import Dispatcher
    from digital_development_team.tools.harness_fields import HarnessFields
    from digital_development_team.tools.mcp_client import McpPool, snapshot_ops
    from digital_development_team.tools.memory import MemoryWiring

    catalog = ToolCatalog(rt.resolver)
    scratch = rt.layout.state_dir / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    mcp_cfg = rt.config.policy.mcp_client
    mcp_ops, mcp_warnings = snapshot_ops(mcp_cfg, rt.layout.state_dir)
    for w in mcp_warnings:
        log.warning(w)
    catalog.add_ops(mcp_ops)
    mcp_pool = (
        McpPool(
            mcp_cfg, mcp_ops, cwd=rt.repo or rt.layout.home, scratch=scratch, network=rt.config.policy.network
        )
        if mcp_ops
        else None
    )
    sandbox = build_sandbox(rt.config.policy.sandbox, scratch=scratch)
    artifacts = ArtifactStore(rt.layout)
    git = git_for(repo or rt.repo, scratch)
    harness_fields = HarnessFields(
        store_dir=rt.layout.state_dir / "harness_fields"
    )  # chia sẻ với process `ddt mcp`
    registry, bb = default_tools(
        catalog,
        store=rt.store,
        artifacts=artifacts,
        sandbox=sandbox,
        sandbox_cfg=rt.config.policy.sandbox,
        git=git,
        base_ref=base_ref,
        harness_fields=harness_fields,
        memory_dir=rt.layout.memory_dir,
        impact_thresholds=rt.config.policy.codegraph_impact_thresholds,
        network=rt.config.policy.network,
        web_cache=rt.layout.home / "cache" / "web",
        ask_role=_ask_role(rt),
        search_backends=_search_backends(rt),
        skills=SkillIndex(SkillRegistry(rt.resolver)),
        memory=MemoryWiring(rt.db, rt.layout, rt.config.policy.memory, rt.config.policy.command_guard),
        mcp=mcp_pool,
    )
    policy = PolicyEngine(rt.config.policy, rt.config.sensitive_paths, catalog)
    dispatcher = Dispatcher(
        catalog,
        registry,
        policy,
        rt.sink,
        artifacts,
        artifactize_threshold=rt.config.policy.condenser_defaults.artifactize_threshold,
    )
    return Tooling(
        catalog=catalog,
        dispatcher=dispatcher,
        blackboard=bb,
        policy=policy,
        sandbox=sandbox,
        artifacts=artifacts,
        git=git,
        sandbox_cfg=rt.config.policy.sandbox,
        harness_fields=harness_fields,
    )

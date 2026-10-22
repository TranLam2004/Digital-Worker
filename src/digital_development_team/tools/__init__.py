"""Tools — 10 namespace (40-tools). Catalog (YAML) = hợp đồng; impl Python đăng ký vào ToolRegistry;
Dispatcher = policy.decide → run → policy.after → artifactize → emit tool_call/tool_denied.

    catalog = ToolCatalog(resolver)
    registry = default_tools(svc...)                 # fs · blackboard · git · exec · test · stubs
    disp = Dispatcher(catalog, registry, policy_engine, sink, artifacts)
    result = disp.dispatch(call, spec, ctx, pctx)    # ToolResult — agent nhận to_model_text()
"""

from __future__ import annotations

from digital_development_team.tools.artifacts import ArtifactStore
from digital_development_team.tools.base import ToolRegistry
from digital_development_team.tools.catalog import ToolCatalog, ToolOp
from digital_development_team.tools.dispatch import Dispatcher

__all__ = ["ArtifactStore", "Dispatcher", "ToolCatalog", "ToolOp", "ToolRegistry"]

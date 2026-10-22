"""Tools qua Dispatcher thật (policy → run → after → emit) trên worktree mini_calc."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from digital_development_team.agents import AgentRegistry
from digital_development_team.app import Runtime, Tooling, build_runtime, build_tooling
from digital_development_team.cli.main import app
from digital_development_team.core.context import Actor, CancelToken, FakeClock, ToolContext
from digital_development_team.core.enums import EventType, Stage, Tier
from digital_development_team.core.models import ToolCall
from digital_development_team.eventlog.reader import read_events
from digital_development_team.policy.engine import PolicyContext
from digital_development_team.tools.base import ToolRegistry
from digital_development_team.tools.catalog import ToolCatalog
from tests.unit.blackboard.test_store import SPEC
from tests.unit.isolation.test_git import make_repo
from tests.unit.runner.test_runner import _runner

ISSUE = "DDT-0001"


@pytest.fixture
def env(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock):  # type: ignore[no-untyped-def]
    git, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo)
    _runner(rt).run(ISSUE, cancel=CancelToken(), until=Stage.SPEC)  # intake · plan · spec có sẵn (stub)
    wt = tmp_path / "wt"
    git.worktree_add(wt, branch=f"ddt/{ISSUE}", base="main")
    tooling = build_tooling(rt, base_ref=lambda c: "main")
    agents = AgentRegistry(rt.resolver)
    yield rt, tooling, agents, wt
    rt.close()


def _dispatch(
    rt: Runtime, tooling: Tooling, agents: AgentRegistry, wt: Path, role: str, name: str, **args: Any
):  # type: ignore[no-untyped-def]
    spec = agents.get(role)
    allowed = tuple(SPEC["allowed_files"])
    actor = Actor(role=role)
    call = ToolCall(
        id="c", name=name, args=args, actor=actor, issue_id=ISSUE, stage=spec.primary_stage or Stage.IMPLEMENT
    )
    tctx = ToolContext(
        issue_id=ISSUE,
        actor=actor,
        stage=call.stage,
        worktree=str(wt),
        home=str(rt.home),
        allowed_files=allowed,
        cancel=CancelToken(),
        clock=rt.clock,
    )
    spec_art = rt.store.read(ISSUE, "spec.yaml").content
    pctx = PolicyContext(
        tier=Tier.T1,
        worktree=str(wt),
        allowed_files=allowed,
        spec_values={
            "spec.test_command": spec_art["test_command"],
            "spec.regression_command": spec_art.get("regression_command"),
        },
    )
    return tooling.dispatcher.dispatch(call, spec, tctx, pctx)


def _tool_events(rt: Runtime) -> list[tuple[str, str, str]]:
    return [
        (e.event.value, e.tool.name, e.tool.status or "") for e in read_events(rt.layout, ISSUE) if e.tool
    ]


def test_catalog_and_registry_complete(env) -> None:  # type: ignore[no-untyped-def]
    _, tooling, agents, _wt = env
    cat: ToolCatalog = tooling.catalog
    assert {
        "fs.read",
        "exec.run",
        "test.run",
        "git.commit",
        "blackboard.write",
        "user.ask",
        "llm.ask",
        "agent.delegate",
        "memory.search",
        "codegraph.search",
    } <= set(cat.names())
    reg: ToolRegistry = tooling.dispatcher._registry
    assert reg.missing_impls() == []
    for a in agents.pipeline_agents():
        specs = cat.specs_for(a.tools)
        assert len(specs) == len({t.split(":")[0] for t in a.tools}) and all(
            s.wire_name.replace("_", ".", 1) in cat.names() or "." in s.name for s in specs
        )
    wire = cat.get("fs.read").tool_spec()
    assert (
        wire.wire_name == "fs_read"
        and wire.input_schema["required"] == ["path"]
        and wire.input_schema["additionalProperties"] is False
    )


def test_fs_tools_roundtrip(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.list", path=".", skeleton=True)
    assert r.status == "ok" and any(
        e["path"] == "calc.py" and "def mean(xs: list[float]) -> float" in e.get("symbols", [])
        for e in r.structured["entries"]
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.read", path="calc.py")
    assert r.status == "ok" and "def mean" in r.structured["content"] and r.structured["lang"] == "python"
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.read", path="calc.py", lines="9-10")
    assert r.status == "ok" and r.structured["lines_returned"] <= 12 and "sum(xs)" in r.structured["content"]
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.search", pattern="len(xs)")
    assert r.status == "ok" and r.structured["count"] >= 1 and r.structured["matches"][0]["path"] == "calc.py"
    r = _dispatch(
        rt,
        tooling,
        agents,
        wt,
        "dev",
        "fs.edit",
        path="calc.py",
        old="    return sum(xs) / len(xs)",
        new="    if not xs:\n        raise ValueError('mean of empty list')\n    return sum(xs) / len(xs)",
    )
    assert r.status == "ok" and r.structured["replacements"] == 1
    assert "raise ValueError('mean of empty list')" in (wt / "calc.py").read_text(encoding="utf-8")
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.edit", path="calc.py", old="KHÔNG CÓ", new="x")
    assert r.status == "error" and r.error is not None and r.error.code == "EDIT_NO_MATCH"
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.edit", path="calc.py", old="float", new="int")
    assert r.status == "error" and r.error is not None and r.error.code == "EDIT_AMBIGUOUS"
    r = _dispatch(
        rt,
        tooling,
        agents,
        wt,
        "dev",
        "fs.write",
        path="tests/test_calc.py",
        content="import pytest\nfrom calc import mean\n\n\ndef test_mean_empty():\n    with pytest.raises(ValueError):\n        mean([])\n",
    )
    assert r.status == "ok" and r.structured["created"] is False
    # ngoài allowlist → denied + event tool_denied
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.write", path="other.py", content="x")
    assert (
        r.status == "denied" and r.structured["rule"] == "scope_violation" and not (wt / "other.py").exists()
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.read", path="../repo/calc.py")
    assert r.status == "denied"
    evs = _tool_events(rt)
    assert ("tool_denied", "fs.write", "denied") in evs and ("tool_call", "fs.edit", "ok") in evs


def test_exec_and_test_tools(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    r = _dispatch(
        rt, tooling, agents, wt, "dev", "exec.run", cmd="python -c \"print('hi'); import sys; sys.exit(3)\""
    )
    assert (
        r.status == "ok"
        and r.structured["exit_code"] == 3
        and "hi" in r.structured["stdout"]
        and "stdout_tail" not in r.structured
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "exec.run", cmd="rm -rf /tmp/x")
    assert r.status == "denied"
    # test.run Dev: pytest thật trên worktree (bug mean([]) chưa sửa → test hiện tại vẫn pass 4)
    r = _dispatch(rt, tooling, agents, wt, "dev", "test.run", cmd="pytest -q")
    assert r.status == "ok", r.summary
    assert (
        r.structured["runner"] == "pytest"
        and r.structured["passed"] == 4
        and r.structured["status"] == "pass"
    )
    assert r.artifact_refs and tooling.artifacts.read(r.artifact_refs[0]).startswith("$ pytest -q")
    assert "passed in" in r.structured["summary_line"]
    # Verifier: chỉ lệnh trong spec; readonly
    r = _dispatch(rt, tooling, agents, wt, "verifier", "test.run", cmd="pytest -k test_add")
    assert r.status == "denied"
    r = _dispatch(rt, tooling, agents, wt, "verifier", "test.run")  # mặc định spec.test_command
    assert (
        r.status == "ok" and r.structured["cmd"] == rt.store.read(ISSUE, "spec.yaml").content["test_command"]
    ), r.summary
    original = (wt / "calc.py").read_text(encoding="utf-8")
    r = _dispatch(
        rt, tooling, agents, wt, "verifier", "exec.run", cmd="python -c \"open('calc.py','a').write('# x')\""
    )
    assert r.status == "denied" and r.structured["rule"] == "cmd_guard:readonly_write"
    # ghi qua script (bộ kiểm lệnh không đọc được nội dung) → phát hiện sau khi chạy và tự hoàn tác từ HEAD
    (wt / "probe_w.py").write_text(
        "open('calc.py', 'a').write('# x')\nopen('new.txt', 'w').write('x')\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(wt), "add", "probe_w.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "probe"],
        check=True,
        capture_output=True,
    )
    r = _dispatch(rt, tooling, agents, wt, "verifier", "exec.run", cmd="python probe_w.py")
    assert r.status == "error" and r.error is not None and r.error.code == "EROFS"
    assert {"calc.py", "new.txt"} <= set(r.structured["fs_writes"]) and set(r.structured["reverted"]) >= {
        "calc.py",
        "new.txt",
    }
    assert (wt / "calc.py").read_text(encoding="utf-8") == original and not (wt / "new.txt").exists()
    # Dev đi vòng qua test.run để chạy lệnh khác: bị chặn (trước đây test.run không qua deny-list)
    r = _dispatch(
        rt, tooling, agents, wt, "dev", "test.run", cmd="git remote -v && git push origin HEAD:main"
    )
    assert r.status == "denied" and r.structured["rule"] == "cmd_guard:git_write"
    r = _dispatch(rt, tooling, agents, wt, "dev", "test.run", cmd="python tools/anything.py")
    assert r.status == "denied" and r.structured["rule"] == "test_runner_only"
    r = _dispatch(rt, tooling, agents, wt, "security", "exec.run", cmd="python -c 1")
    assert r.status == "denied" and r.structured["rule"] == "scan_allowlist"
    r = _dispatch(rt, tooling, agents, wt, "dev", "test.run", cmd="pytest -q || true")
    assert r.status == "denied"


def test_git_and_blackboard_tools(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    r = _dispatch(
        rt, tooling, agents, wt, "dev", "blackboard.write", slot="patch/", content={"impl_log": "chưa sửa gì"}
    )
    assert (
        r.status == "error"
        and r.error is not None
        and r.error.code == "DIFF_SANITY"
        and "rỗng" in r.error.message
    )
    _dispatch(
        rt,
        tooling,
        agents,
        wt,
        "dev",
        "fs.edit",
        path="calc.py",
        old="    return sum(xs) / len(xs)",
        new="    if not xs:\n        raise ValueError('mean of empty list')\n    return sum(xs) / len(xs)",
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "git.status")
    assert (
        r.status == "ok"
        and r.structured["modified"] == ["calc.py"]
        and r.structured["branch"] == f"ddt/{ISSUE}"
    )
    r = _dispatch(rt, tooling, agents, wt, "dev", "git.diff")
    assert (
        r.status == "ok"
        and r.structured["files"][0]["path"] == "calc.py"
        and "+        raise ValueError" in r.structured["diff"]
    )
    r = _dispatch(rt, tooling, agents, wt, "leader", "git.diff")
    assert (
        r.status == "denied" and r.structured["rule"] == "allowlist"
    )  # Leader không có git.*, xem stat qua blackboard
    r = _dispatch(rt, tooling, agents, wt, "dev", "git.diff", stat=True)
    assert r.status == "ok" and r.structured["diff"] == "" and r.structured["total_added"] == 2
    r = _dispatch(rt, tooling, agents, wt, "dev", "git.commit", message="guard empty list")
    assert r.status == "ok" and r.structured["files"] == 1
    assert tooling.git.log(cwd=wt, n=1)[0]["message"] == f"ddt({ISSUE}): guard empty list"
    r = _dispatch(rt, tooling, agents, wt, "dev", "git.commit", message="again")
    assert r.status == "error" and r.error is not None and r.error.code == "NOTHING_TO_COMMIT"
    # patch/ từ git: diff + files_touched + base/head
    r = _dispatch(
        rt,
        tooling,
        agents,
        wt,
        "dev",
        "blackboard.write",
        slot="patch/",
        content={"impl_log": "Thêm guard rỗng", "handoff": "test mới ở tests/test_calc.py"},
    )
    assert r.status == "ok" and r.structured["version"] == 1, r.summary
    patch = rt.store.read(ISSUE, "patch/").content
    assert (
        patch["files_touched"] == ["calc.py"]
        and "+        raise ValueError" in patch["diff"]
        and len(patch["head_commit"]) >= 7
    )
    # blackboard.read ACL: dev không đọc intake; verifier đọc patch/; schema error có details + attempt
    r = _dispatch(rt, tooling, agents, wt, "dev", "blackboard.read", slot="intake.yaml")
    assert r.status == "denied" and r.structured["rule"] == "slot_acl"
    r = _dispatch(
        rt, tooling, agents, wt, "verifier", "blackboard.read", slot="spec.yaml", field="test_command"
    )
    assert r.status == "ok" and r.structured["content"] == {
        "test_command": rt.store.read(ISSUE, "spec.yaml").content["test_command"]
    }
    r = _dispatch(
        rt,
        tooling,
        agents,
        wt,
        "verifier",
        "blackboard.write",
        slot="evidence.json",
        content={"issue_id": ISSUE, "verdict": "pass"},
    )
    assert (
        r.status == "error"
        and r.error is not None
        and r.error.code == "SCHEMA"
        and r.error.details
        and "1/3" in (r.error.hint or "")
    )
    for _ in range(3):
        r = _dispatch(
            rt,
            tooling,
            agents,
            wt,
            "verifier",
            "blackboard.write",
            slot="evidence.json",
            content={"issue_id": ISSUE},
        )
    assert r.status == "error" and r.error is not None and r.error.code == "TOO_MANY_ATTEMPTS"
    r = _dispatch(rt, tooling, agents, wt, "dev", "blackboard.list")
    assert (
        r.status == "denied" and r.structured["rule"] == "allowlist"
    )  # không agent nào có blackboard.list (chỉ harness/CLI)
    # codegraph: repo chưa index CodeGraph → fallback PyGraph (ast) vẫn trả lời; stub còn lại
    r = _dispatch(rt, tooling, agents, wt, "dev", "codegraph.search", query="mean")
    assert r.status == "ok" and r.structured["backend"] == "pyast"
    r = _dispatch(rt, tooling, agents, wt, "dev", "memory.search", query="mean")
    assert r.status == "ok" and r.structured["count"] == 0
    types = [e.event for e in read_events(rt.layout, ISSUE)]
    assert EventType.TOOL_CALL in types and EventType.TOOL_DENIED in types and EventType.SLOT_WRITTEN in types


def test_artifactize_large_output(env) -> None:  # type: ignore[no-untyped-def]
    rt, tooling, agents, wt = env
    big = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    (wt / "calc.py").write_text(big, encoding="utf-8")
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.read", path="calc.py")
    assert r.status == "ok" and r.artifact_refs and r.preview and r.structured["lines_total"] == 400
    r2 = _dispatch(rt, tooling, agents, wt, "dev", "fs.read", path=r.artifact_refs[0], lines="100-101")
    assert r2.status == "ok" and "line 100" in r2.structured["content"]


def test_cli_debug_tool(tmp_home: Path, tmp_path: Path) -> None:
    _, repo = make_repo(tmp_path)
    cli = CliRunner()
    base = ["--home", str(tmp_home), "--repo", str(repo)]
    assert cli.invoke(app, [*base, "run", ISSUE, "--stub-stages", "--until", "SPEC"]).exit_code == 0
    r = cli.invoke(
        app,
        [
            *base,
            "debug",
            "tool",
            ISSUE,
            "fs.read",
            "--role",
            "dev",
            "--args",
            json.dumps({"path": "calc.py"}),
        ],
    )
    assert r.exit_code == 0 and "def mean" in r.output, r.output
    r = cli.invoke(
        app,
        [
            *base,
            "debug",
            "tool",
            ISSUE,
            "exec.run",
            "--role",
            "dev",
            "--args",
            json.dumps({"cmd": "git push"}),
        ],
    )
    assert r.exit_code == 0 and "denied" in r.output


def test_normalize_plan_files() -> None:
    from digital_development_team.tools.blackboard import normalize_slot

    plan = {
        "candidates": [{"rank": 1, "files": ["shop/cart.py", {"path": "tests/test_cart.py"}]}],
        "steps": [],
    }
    out = normalize_slot("plan.yaml", plan)
    assert out["candidates"][0]["files"] == [{"path": "shop/cart.py"}, {"path": "tests/test_cart.py"}]
    assert normalize_slot("spec.yaml", plan) is plan


def test_budget_tokens_weight_cache_reads() -> None:
    from digital_development_team.core.models import BudgetUsage

    u = BudgetUsage(tokens_in=20_000, tokens_out=100, tokens_cached=18_000)
    assert u.tokens == 2_000 + 1_800 + 100  # uncached + cache-read × 0.1 + out
    assert BudgetUsage(tokens_in=500, tokens_out=50).tokens == 550


def test_preview_never_exceeds_toolresult_limit() -> None:
    from digital_development_team.core.models import ToolResult
    from digital_development_team.tools.artifacts import PREVIEW_MAX_LINES, ArtifactStore

    long = "\n".join(f"line {i}" for i in range(500))
    for head, tail in ((10, 10), (5, 15), (0, 30), (25, 25)):
        p = ArtifactStore.preview(long, head, tail)
        assert len(p) <= PREVIEW_MAX_LINES, (head, tail, len(p))
        ToolResult(status="ok", summary="x", preview=p)  # không ValidationError
    assert ArtifactStore.preview("a\nb") == ("a", "b")


def test_harness_fields_shared_across_processes(tmp_path: Path) -> None:
    """Runner đặt field; một HarnessFields khác (process MCP) cùng store_dir đọc được và merge thắng agent."""
    from digital_development_team.tools.harness_fields import HarnessFields

    runner = HarnessFields(store_dir=tmp_path)
    runner.set("DDT-0001", "evidence.json", {"version": 2, "machine_gate": {"unit_test": {"status": "pass"}}})
    mcp = HarnessFields(store_dir=tmp_path)
    merged = mcp.merge(
        "DDT-0001", "evidence.json", {"verdict": "pass", "machine_gate": {"unit_test": "pass"}}
    )
    assert (
        merged["version"] == 2
        and merged["machine_gate"] == {"unit_test": {"status": "pass"}}
        and merged["verdict"] == "pass"
    )
    runner.set("DDT-0001", "patch/", {"x": 1})
    assert mcp.get("DDT-0001", "patch/") == {"x": 1}
    runner.clear("DDT-0001")
    assert mcp.get("DDT-0001", "evidence.json") == {} and mcp.get("DDT-0001", "patch/") == {}
    assert HarnessFields().get("DDT-0001", "evidence.json") == {}  # không store_dir → chỉ bộ nhớ


def test_fs_search_retries_regex_looking_literal(env) -> None:  # type: ignore[no-untyped-def]
    """Live DDT-0003: agent gửi "a|b" không kèm regex=true → 0 match. Literal 0 match + cú pháp regex → thử lại dạng regex."""
    rt, tooling, agents, wt = env
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.search", pattern="def mean|def nothing_here")
    assert r.status == "ok" and r.structured["count"] >= 1 and r.structured.get("regex") is True
    assert "retried as regex" in r.summary
    r = _dispatch(
        rt, tooling, agents, wt, "dev", "fs.search", pattern="len(xs)"
    )  # literal có match → không đổi
    assert r.structured["count"] >= 1 and "regex" not in r.structured
    r = _dispatch(rt, tooling, agents, wt, "dev", "fs.search", pattern="no_such_symbol_xyz")
    assert r.structured["count"] == 0 and "regex" not in r.structured

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from digital_development_team.app import Runtime
from digital_development_team.blackboard import SLOTS, SlotAcl
from digital_development_team.core.context import Actor
from digital_development_team.core.errors import SchemaError, SlotPermissionError, StoreError
from digital_development_team.core.models import PatchBundle
from digital_development_team.eventlog.reader import read_events

ISSUE = "DDT-0001"
INTAKE: dict[str, Any] = {
    "issue_id": ISSUE,
    "type": "bug",
    "severity": "P2",
    "complexity": "small",
    "risk_tier": "T1",
    "route": "standard",
    "confidence": 0.84,
    "affected_area": "calc",
    "entry_hints": ["mean"],
    "rationale": "Stack trace rõ.",
}
SPEC: dict[str, Any] = {
    "issue_id": ISSUE,
    "goal": "mean([]) không crash",
    "acceptance_criteria": [
        {
            "id": "AC1",
            "text": "mean([]) raise ValueError",
            "verify_by": "test",
            "test_ref": "tests/test_calc.py::test_mean_empty",
        }
    ],
    "allowed_files": ["calc.py", "tests/test_calc.py"],
    "test_command": "pytest -q",
    "rollback_plan": "git revert HEAD",
}


def _events(rt: Runtime) -> list[str]:
    return [e.event.value for e in read_events(rt.layout, ISSUE)]


def test_write_read_versions_and_latest_copy(runtime: Runtime) -> None:
    rt = runtime
    r1 = rt.store.write(ISSUE, "spec.yaml", SPEC, actor=Actor(role="analyst"))
    assert r1.version == 1 and r1.uri == f"blackboard://{ISSUE}/spec.yaml@1"
    assert r1.path == f"work/{ISSUE}/spec.v1.yaml"
    assert rt.layout.slot_latest_path(ISSUE, "spec.yaml").is_file()

    spec2 = {**SPEC, "goal": "v2"}
    r2 = rt.store.write(ISSUE, "spec.yaml", spec2, actor=Actor(role="analyst"))
    assert r2.version == 2
    assert rt.store.read(ISSUE, "spec.yaml").content["goal"] == "v2"
    assert rt.store.read(ISSUE, "spec.yaml", version=1).content["goal"] == SPEC["goal"]
    assert rt.store.latest(ISSUE, "spec.yaml") == r2
    assert [r.version for r in rt.store.list(ISSUE)] == [1, 2]
    # bản v1 vẫn nguyên trên đĩa (bất biến)
    assert (rt.home / r1.path).is_file()
    assert _events(rt) == ["slot_written", "slot_versioned", "slot_written"]
    evs = read_events(rt.layout, ISSUE)
    assert evs[1].data == {"slot": "spec.yaml", "from": 1, "to": 2}
    assert evs[2].artifact_ref == r2.uri and evs[2].hash == r2.sha256


def test_schema_fail_emits_rejected_and_writes_nothing(runtime: Runtime) -> None:
    rt = runtime
    bad = {**INTAKE, "confidence": 2}
    with pytest.raises(SchemaError) as ei:
        rt.store.write(ISSUE, "intake.yaml", bad, actor=Actor(role="intake"))
    assert ei.value.brief()[0]["path"] == "confidence"
    assert rt.store.latest(ISSUE, "intake.yaml") is None
    assert not rt.layout.slot_versioned_path(ISSUE, "intake.yaml", 1).exists()
    evs = read_events(rt.layout, ISSUE)
    assert [e.event.value for e in evs] == ["slot_rejected"]
    assert evs[0].data["errors"][0]["path"] == "confidence"


def test_acl_write_denied(runtime: Runtime) -> None:
    with pytest.raises(SlotPermissionError):
        runtime.store.write(ISSUE, "spec.yaml", SPEC, actor=Actor(role="dev"))
    assert runtime.store.latest(ISSUE, "spec.yaml") is None
    assert _events(runtime) == []  # tool layer emit tool_denied, store không emit


def test_read_as_projection(runtime: Runtime) -> None:
    rt = runtime
    ev = rt.registry.schema("evidence")["examples"][0]
    ev = {
        **ev,
        "issue_id": ISSUE,
        "verdict": "fail",
        "failure_feedback_for_dev": "calc.py:12 thiếu guard rỗng",
    }
    ev["criteria"] = [{**ev["criteria"][0], "result": "fail"}, *ev["criteria"][1:]]
    rt.store.write(ISSUE, "evidence.json", ev, actor=Actor(role="verifier"))
    dev_view = rt.store.read_as(ISSUE, "evidence.json", Actor(role="dev", sub_id="s1")).content
    assert set(dev_view) <= {"issue_id", "version", "verdict", "failure_feedback_for_dev", "scope_violation"}
    assert dev_view["failure_feedback_for_dev"].startswith("calc.py:12")
    leader_view = rt.store.read_as(ISSUE, "evidence.json", Actor(role="leader")).content
    assert "criteria" in leader_view
    with pytest.raises(SlotPermissionError):
        rt.store.read_as(ISSUE, "evidence.json", Actor(role="intake"))


def test_patch_bundle_materialized(runtime: Runtime) -> None:
    rt = runtime
    bundle = PatchBundle(
        diff="--- a/calc.py\n+++ b/calc.py\n@@ -1 +1,2 @@\n+if not xs: raise ValueError\n",
        files_touched=("calc.py",),
        impl_log="guard rỗng",
        base_commit="a" * 7,
        head_commit="b" * 7,
    )
    ref = rt.store.write(ISSUE, "patch/", bundle, actor=Actor(role="dev"))
    assert ref.path == f"work/{ISSUE}/patch.v1.json"
    pdir = rt.layout.slot_latest_path(ISSUE, "patch/")
    assert (pdir / "diff.patch").read_text(encoding="utf-8") == bundle.diff
    assert json.loads((pdir / "files_touched.json").read_text(encoding="utf-8")) == ["calc.py"]
    art = rt.store.read(ISSUE, "patch/")
    assert PatchBundle.model_validate(art.content).files_touched == ("calc.py",)
    stat = rt.store.read_as(ISSUE, "patch/", Actor(role="leader")).content
    assert "diff" not in stat and "files_touched" in stat
    # patch rỗng bị chặn cơ học
    with pytest.raises(SchemaError, match="rỗng"):
        rt.store.write(ISSUE, "patch/", {**bundle.model_dump(), "diff": "  "}, actor=Actor(role="dev"))


def test_item_json_immutable(runtime: Runtime) -> None:
    item = runtime.registry.schema("item")["examples"][0]
    runtime.store.write(ISSUE, "item.json", item, actor=Actor(role="runner"))
    with pytest.raises(StoreError, match="bất biến"):
        runtime.store.write(ISSUE, "item.json", item, actor=Actor(role="runner"))


def test_tamper_detected(runtime: Runtime) -> None:
    rt = runtime
    ref = rt.store.write(ISSUE, "intake.yaml", INTAKE, actor=Actor(role="intake"))
    p = rt.home / ref.path
    p.write_text(p.read_text(encoding="utf-8") + "\n# sửa tay", encoding="utf-8")
    with pytest.raises(StoreError, match="sha lệch"):
        rt.store.read(ISSUE, "intake.yaml")


def test_unknown_slot_and_missing(runtime: Runtime) -> None:
    with pytest.raises(StoreError, match="slot lạ"):
        runtime.store.write(ISSUE, "nope.yaml", {}, actor=Actor(role="runner"))
    with pytest.raises(StoreError, match="chưa có"):
        runtime.store.read(ISSUE, "spec.yaml")
    with pytest.raises(StoreError):
        runtime.store.write(ISSUE, "events.jsonl", {}, actor=Actor(role="runner"))


def test_default_acl_matches_design_matrix() -> None:
    acl = SlotAcl()
    assert acl.can_write("analyst", "spec.yaml") and not acl.can_write("dev", "spec.yaml")
    assert acl.can_write("dev", "patch/") and acl.can_read("verifier", "patch/")
    assert not acl.can_read("verifier", "intake.yaml")  # Verifier độc lập: không thấy reasoning upstream
    assert acl.can_read("dev", "evidence.json") and not acl.can_read("dev", "intake.yaml")
    assert acl.can_write("leader", "decision.json") and acl.can_read("runner", "decision.json")
    assert acl.can_read("human:P1", "evidence.json") and not acl.can_write("human:P1", "decision.json")
    assert all(
        acl.can_write(r, "escalation.yaml")
        for r in ("intake", "planner", "analyst", "dev", "verifier", "security", "leader")
    )
    assert not acl.can_write("learner", "decision.json") and acl.can_read("learner", "events.jsonl")
    assert acl.can_append("dev", "events.jsonl") and not acl.can_append("learner", "events.jsonl")
    # mọi slot có schema trừ patch/ và events.jsonl
    assert {s for s, sp in SLOTS.items() if sp.schema_id is None} == {"patch/"} or SLOTS[
        "events.jsonl"
    ].schema_id == "event"
    # with_role từ frontmatter + kiểm vượt ma trận
    acl2 = acl.with_role("dev", reads=["spec.yaml", "intake.yaml"], writes=["patch/"])
    assert acl2.can_read("dev", "intake.yaml")
    assert acl2.violations_against_default("dev") == ["read intake.yaml ngoài ma trận"]
    assert acl2.project("dev", "evidence.json", {"verdict": "fail", "criteria": []}) == {"verdict": "fail"}


def test_write_denied_for_unknown_role(runtime: Runtime, tmp_path: Path) -> None:
    with pytest.raises(SlotPermissionError):
        runtime.store.write(ISSUE, "spec.yaml", SPEC, actor=Actor(role="stranger"))


def test_harness_sets_real_issue_id(runtime: Runtime) -> None:
    """Agent ghi sai issue_id (live: "DDT-0000") hoặc bỏ trống → store đặt id thật, ghi chú trong slot_written."""
    ref = runtime.store.write(
        ISSUE, "intake.yaml", {**INTAKE, "issue_id": "DDT-0000"}, actor=Actor(role="intake")
    )
    assert runtime.store.read(ISSUE, "intake.yaml").content["issue_id"] == ISSUE and ref.version == 1
    ev = [e for e in read_events(runtime.layout, ISSUE) if e.event.value == "slot_written"][-1]
    assert any("issue_id" in n for n in ev.data["coerced"])
    no_id = {k: v for k, v in SPEC.items() if k != "issue_id"}
    runtime.store.write(ISSUE, "spec.yaml", no_id, actor=Actor(role="analyst"))
    assert runtime.store.read(ISSUE, "spec.yaml").content["issue_id"] == ISSUE

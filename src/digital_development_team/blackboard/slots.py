"""Registry slot + ma trận quyền (60-communication/03-slots.md).

SLOTS: cấu trúc (kind, schema, versioned) — thuộc code vì gắn với hành vi lưu trữ.
DEFAULT_ACL: ma trận R/W/A mặc định. Frontmatter agent (reads[]/writes[]) phải ⊆ ma trận này
(contract test ở bước agents); runtime dùng SlotAcl.with_role() để nạp quyền thật của agent.
Cưỡng chế ở blackboard tool (tools/blackboard.py) + store.write (phòng thủ 2 lớp).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from digital_development_team.core.errors import StoreError
from digital_development_team.schema.registry import schema_id_for_slot


class SlotKind(StrEnum):
    FILE = "file"  # 1 file yaml/json, version hoá
    DIR = "dir"  # patch/: lưu PatchBundle json + materialize thư mục cho người đọc
    LOG = "log"  # events.jsonl: append-only, không version


@dataclass(frozen=True, slots=True)
class SlotSpec:
    name: str
    kind: SlotKind
    stage: str  # stage ghi (mô tả, không cưỡng chế ở đây)
    human_readable: bool = False  # người đọc trực tiếp trên dashboard
    description: str = ""

    @property
    def schema_id(self) -> str | None:
        return schema_id_for_slot(self.name)

    @property
    def versioned(self) -> bool:
        return self.kind is not SlotKind.LOG


SLOTS: dict[str, SlotSpec] = {
    s.name: s
    for s in (
        SlotSpec("item.json", SlotKind.FILE, "INGEST", description="Raw từ backlog, harness ghi, bất biến"),
        SlotSpec(
            "match.json",
            SlotKind.FILE,
            "START_WORKING",
            True,
            "Thi đấu: loại item · điểm · hạn · MVS · submit template (harness ghi từ Game Master)",
        ),
        SlotSpec(
            "intake.yaml", SlotKind.FILE, "TRIAGE", description="Phân loại: type · severity · tier · route"
        ),
        SlotSpec("plan.yaml", SlotKind.FILE, "LOCALIZE", description="Sửa ở đâu: candidates ≤3 · steps"),
        SlotSpec("spec.yaml", SlotKind.FILE, "SPEC", True, "Hợp đồng: AC ≤8 · allowed_files · test_command"),
        SlotSpec("patch/", SlotKind.DIR, "IMPLEMENT", True, "diff.patch · files_touched.json · impl_log.md"),
        SlotSpec(
            "progress.yaml",
            SlotKind.FILE,
            "IMPLEMENT",
            True,
            "Dev ghi tiến độ: việc đã xong · đã biết · bước kế",
        ),
        SlotSpec(
            "evidence.json", SlotKind.FILE, "VERIFY", True, "verdict · criteria · regression · findings"
        ),
        SlotSpec("sec_report.json", SlotKind.FILE, "VERIFY", True, "mode · verdict · checklist · findings"),
        SlotSpec(
            "decision.json", SlotKind.FILE, "GATE", True, "action · final_tier · confidence · requires_human"
        ),
        SlotSpec(
            "escalation.yaml", SlotKind.FILE, "*", True, "Hỏi người: question · options ≥2 · recommendation"
        ),
        SlotSpec("clarify.yaml", SlotKind.FILE, "*", description="Hỏi đồng nghiệp 1 vòng"),
        SlotSpec(
            "answer.yaml",
            SlotKind.FILE,
            "ANSWER",
            True,
            "Trả lời issue loại câu hỏi: answer · references · follow_up",
        ),
        SlotSpec(
            "blocked_report.json",
            SlotKind.FILE,
            "BLOCKED",
            True,
            "Harness ghi khi BLOCKED: category · đã thử gì · phần việc giữ lại · cách gỡ",
        ),
        SlotSpec(
            "learn_report.json",
            SlotKind.FILE,
            "DONE",
            True,
            "Learner ghi sau khi học từ issue đã xong: lesson · fact · skill đã lưu",
        ),
        SlotSpec("events.jsonl", SlotKind.LOG, "*", description="Audit log append-only"),
    )
}


def slot_spec(slot: str) -> SlotSpec:
    try:
        return SLOTS[slot]
    except KeyError:
        raise StoreError(f"slot lạ: {slot!r}", hint=f"có: {sorted(SLOTS)}") from None


# ───── ACL ─────
@dataclass(frozen=True, slots=True)
class Projection:
    """Đọc một phần: only = chỉ các field này · without = bỏ các field này."""

    only: tuple[str, ...] = ()
    without: tuple[str, ...] = ()

    def apply(self, content: Mapping[str, Any]) -> dict[str, Any]:
        if self.only:
            return {k: v for k, v in content.items() if k in self.only}
        if self.without:
            return {k: v for k, v in content.items() if k not in self.without}
        return dict(content)


@dataclass(frozen=True, slots=True)
class RoleAccess:
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    appends: frozenset[str] = frozenset()
    projections: Mapping[str, Projection] = field(default_factory=dict)


_FEEDBACK_ONLY = Projection(
    only=("issue_id", "version", "verdict", "failure_feedback_for_dev", "scope_violation")
)
_PATCH_STAT = Projection(without=("diff",))
_MATCH_WORK = Projection(
    without=("submit_template", "submission")
)  # agent cần MVS/hạn, không cần chuyện nộp bài
_EV = frozenset({"events.jsonl"})


def _ra(
    reads: str = "", writes: str = "", *, appends: bool = True, proj: Mapping[str, Projection] | None = None
) -> RoleAccess:
    return RoleAccess(
        reads=frozenset(reads.split()),
        writes=frozenset(writes.split()),
        appends=_EV if appends else frozenset(),
        projections=dict(proj or {}),
    )


DEFAULT_ACL: dict[str, RoleAccess] = {
    # agent
    "intake": _ra(
        "item.json match.json clarify.yaml",
        "intake.yaml escalation.yaml clarify.yaml",
        proj={"match.json": _MATCH_WORK},
    ),
    "planner": _ra(
        "intake.yaml match.json clarify.yaml",
        "plan.yaml escalation.yaml clarify.yaml",
        proj={"match.json": _MATCH_WORK},
    ),
    "analyst": _ra(
        "intake.yaml plan.yaml match.json clarify.yaml",
        "spec.yaml escalation.yaml clarify.yaml",
        proj={"match.json": _MATCH_WORK},
    ),
    "dev": _ra(
        "plan.yaml spec.yaml match.json evidence.json clarify.yaml progress.yaml",
        "patch/ answer.yaml escalation.yaml clarify.yaml progress.yaml",
        proj={"evidence.json": _FEEDBACK_ONLY, "match.json": _MATCH_WORK},
    ),
    "verifier": _ra(
        "spec.yaml patch/ match.json", "evidence.json escalation.yaml", proj={"match.json": _MATCH_WORK}
    ),
    "security": _ra(
        "intake.yaml spec.yaml patch/ clarify.yaml", "sec_report.json escalation.yaml clarify.yaml"
    ),
    "leader": _ra(
        "intake.yaml plan.yaml spec.yaml patch/ evidence.json sec_report.json clarify.yaml escalation.yaml answer.yaml match.json",
        "decision.json escalation.yaml",
        proj={"patch/": _PATCH_STAT},
    ),
    "learner": _ra(
        "intake.yaml plan.yaml spec.yaml patch/ progress.yaml evidence.json sec_report.json decision.json clarify.yaml answer.yaml blocked_report.json events.jsonl",
        "learn_report.json",
        appends=False,
    ),
    # harness
    "runner": _ra(
        "item.json match.json decision.json escalation.yaml clarify.yaml answer.yaml progress.yaml",
        "item.json match.json escalation.yaml decision.json evidence.json spec.yaml blocked_report.json intake.yaml",
    ),  # intake.yaml: self-consistency giữ bản thận trọng · decision.json: GATE rule-only T0 · evidence.json: inconclusive khi Verifier blocked 2 lần · spec.yaml: spec_patch
    "human": _ra(
        "intake.yaml spec.yaml patch/ progress.yaml evidence.json sec_report.json decision.json escalation.yaml clarify.yaml answer.yaml blocked_report.json learn_report.json match.json events.jsonl",
        "escalation.yaml clarify.yaml",
        appends=False,
        proj={"patch/": _PATCH_STAT},
    ),
}
"""Người ghi escalation/clarify = trả lời (resolved/answer) — qua CLI/gate, không trực tiếp."""

HARNESS_ROLES = frozenset({"runner", "human", "learner"})


class SlotAcl:
    def __init__(self, access: Mapping[str, RoleAccess] | None = None) -> None:
        self._access: dict[str, RoleAccess] = dict(access or DEFAULT_ACL)

    @staticmethod
    def _role(actor_role: str) -> str:
        # human:P1 → human · dev:sub1 → dev
        return actor_role.split(":", 1)[0]

    def access(self, role: str) -> RoleAccess:
        return self._access.get(self._role(role), RoleAccess())

    def can_read(self, role: str, slot: str) -> bool:
        return slot in self.access(role).reads

    def can_write(self, role: str, slot: str) -> bool:
        return slot in self.access(role).writes

    def can_append(self, role: str, slot: str) -> bool:
        return slot in self.access(role).appends

    def readable_slots(self, role: str) -> list[str]:
        return [s for s in SLOTS if self.can_read(role, s)]

    def project(self, role: str, slot: str, content: Mapping[str, Any]) -> dict[str, Any]:
        proj = self.access(role).projections.get(slot)
        return proj.apply(content) if proj else dict(content)

    def with_role(self, role: str, *, reads: Iterable[str], writes: Iterable[str]) -> SlotAcl:
        """Nạp quyền từ frontmatter agent; giữ projection mặc định của role đó nếu có."""
        base = self._access.get(role, RoleAccess())
        new = replace(base, reads=frozenset(reads), writes=frozenset(writes))
        merged = dict(self._access)
        merged[role] = new
        return SlotAcl(merged)

    def violations_against_default(self, role: str) -> list[str]:
        """Quyền của role vượt ma trận mặc định → liệt kê (contract test / config check)."""
        cur, ref = self.access(role), DEFAULT_ACL.get(role)
        if ref is None:
            return [f"role {role!r} không có trong ma trận mặc định"]
        out = [f"read {s} ngoài ma trận" for s in sorted(cur.reads - ref.reads)]
        out += [f"write {s} ngoài ma trận" for s in sorted(cur.writes - ref.writes)]
        return out

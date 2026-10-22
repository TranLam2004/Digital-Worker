"""Sửa spec giữa chừng — đường DUY NHẤT để nới `allowed_files` / đổi lệnh test sau SPEC.

Trước đây spec khóa cứng: người duyệt "cho sửa thêm file X" trên escalation cũng không đổi spec.yaml → lời gọi lại bị
chặn → hỏi lại → TOO_MANY_ASKS → BLOCKED.

spec_patch (trong option của escalation.yaml): {allowed_files_add: [...], test_command?, regression_command?}
  kiểm luôn  path an toàn (tương đối, không `..`), thư mục cha tồn tại trong repo, tổng ≤ max_allowed_files,
             lệnh mới qua command guard
  tự duyệt   (không cần người) chỉ khi: CHỈ thêm file (không đổi lệnh — đổi lệnh test = đổi thước đo), ≤ auto_max_paths,
             mỗi file là file test hoặc cùng thư mục với một file đã được phép, không nhạy cảm, ≤ auto_max_per_issue lần
  người duyệt (G0/G1) được thêm file nhạy cảm (tier rules vẫn tính) và đổi lệnh.
Runner ghi spec.yaml bản mới (amendments[] giữ lịch sử) và emit spec_amended.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from digital_development_team.blackboard.store import FileStore
from digital_development_team.config.models import CommandGuardCfg, SpecAmendCfg
from digital_development_team.core.context import Actor
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import DDTError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.protocols import EventSink
from digital_development_team.policy.command import check_command
from digital_development_team.util.globs import GlobSet
from digital_development_team.util.testpaths import is_test_path

SLOT = "spec.yaml"
COMMAND_KEYS = ("test_command", "regression_command")


@dataclass
class AmendResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    version: int | None = None


def patch_of(option: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """spec_patch của một option escalation; rỗng/không có → None."""
    raw = option.get("spec_patch") if isinstance(option, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    files = [str(p).strip() for p in raw.get("allowed_files_add") or [] if str(p).strip()]
    if files:
        out["allowed_files_add"] = files
    for k in COMMAND_KEYS:
        if str(raw.get(k) or "").strip():
            out[k] = str(raw[k]).strip()
    return out or None


def recommended_option(esc: Mapping[str, Any]) -> Mapping[str, Any] | None:
    rec = str(esc.get("recommendation") or "")
    for o in esc.get("options", []) or []:
        if isinstance(o, Mapping) and str(o.get("id")) == rec:
            return o
    return None


def _norm(p: str) -> str | None:
    q = p.replace("\\", "/").strip()
    while q.startswith("./"):
        q = q[2:]
    pp = PurePosixPath(q)
    if not q or pp.is_absolute() or ":" in q or ".." in pp.parts:
        return None
    return str(pp)


def check_patch(
    patch: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    root: Path,
    guard: CommandGuardCfg,
    max_files: int,
) -> list[str]:
    """Vấn đề chặn mọi spec_patch (kể cả người duyệt)."""
    problems: list[str] = []
    current = [str(x) for x in spec.get("allowed_files", [])]
    new: list[str] = []
    for raw in patch.get("allowed_files_add") or []:
        p = _norm(str(raw))
        if p is None:
            problems.append(f"unsafe path {raw!r}")
            continue
        if not (root / p).parent.is_dir():
            problems.append(f"{p}: parent directory does not exist in the repo")
            continue
        if p not in current and p not in new:
            new.append(p)
    if len(current) + len(new) > max_files:
        problems.append(f"allowed_files would exceed {max_files} files")
    for k in COMMAND_KEYS:
        cmd = patch.get(k)
        if cmd:
            v = check_command(str(cmd), guard, worktree=str(root))
            if not v.ok:
                problems.append(f"{k} blocked by the command guard ({v.rule}): {v.reason}")
    return problems


def auto_approvable(
    patch: Mapping[str, Any],
    spec: Mapping[str, Any],
    cfg: SpecAmendCfg,
    *,
    sensitive_globs: Sequence[str],
    used: int,
) -> bool:
    if not cfg.auto_enabled or used >= cfg.auto_max_per_issue:
        return False
    if any(patch.get(k) for k in COMMAND_KEYS):
        return False  # đổi thước đo → người hoặc Analyst (re_spec) quyết
    files = [p for p in (_norm(str(x)) for x in patch.get("allowed_files_add") or []) if p]
    if not files or len(files) > cfg.auto_max_paths:
        return False
    sens = GlobSet(list(sensitive_globs))
    dirs = {str(PurePosixPath(str(a).replace("\\", "/")).parent) for a in spec.get("allowed_files", [])}
    return all(not sens.match(p) and (is_test_path(p) or str(PurePosixPath(p).parent) in dirs) for p in files)


def amend_spec(
    store: FileStore,
    sink: EventSink,
    issue: str,
    patch: Mapping[str, Any],
    *,
    by: str,
    reason: str,
    root: Path,
    guard: CommandGuardCfg,
    max_files: int,
    at: str,
    stage: Stage | None = None,
    auto: bool = False,
) -> AmendResult:
    try:
        spec = dict(store.read(issue, SLOT).content)
    except StoreError:
        return AmendResult(False, ["spec.yaml not written yet"])
    problems = check_patch(patch, spec, root=root, guard=guard, max_files=max_files)
    if problems:
        return AmendResult(False, problems)
    current = [str(x) for x in spec.get("allowed_files", [])]
    added = [
        p for p in (_norm(str(x)) for x in patch.get("allowed_files_add") or []) if p and p not in current
    ]
    entry: dict[str, Any] = {"by": by, "at": at, "reason": reason[:300]}
    if added:
        entry["allowed_files_add"] = added
        spec["allowed_files"] = current + added
    for k in COMMAND_KEYS:
        if patch.get(k):
            entry[k] = str(patch[k])
            spec[k] = str(patch[k])
    spec["amendments"] = [*list(spec.get("amendments") or []), entry]
    try:
        ref = store.write(issue, SLOT, spec, actor=Actor(role="runner"))
    except DDTError as e:
        return AmendResult(False, [f"spec.yaml rejected: {e.message}"])
    sink.emit(
        EventDraft(
            issue_id=issue,
            actor="runner",
            event=EventType.SPEC_AMENDED,
            stage=stage,
            data={
                "by": by,
                "auto": auto,
                "added": added,
                "commands": [k for k in COMMAND_KEYS if patch.get(k)],
                "version": ref.version,
            },
            artifact_ref=ref.uri,
        )
    )
    return AmendResult(True, [], added, ref.version)


# ───── điểm gắn vào pipeline ─────
def _root(state: Any, ctx: Any) -> Path | None:
    if getattr(state, "worktree", None):
        return Path(state.worktree)
    wts = ctx.svc.worktrees
    return Path(wts.git.root) if wts is not None else None


def auto_amend_from_escalation(state: Any, ctx: Any, stage: Stage) -> bool:
    """Agent vừa nộp escalation mà khuyến nghị của nó chỉ là nới spec trong giới hạn tự duyệt → harness áp luôn,
    ghi câu trả lời 'system' (agent thấy trong brief), KHÔNG mở gate người, KHÔNG tính vào MAX_ASKS.
    True = đã áp, stage chạy lại agent với allowlist mới."""
    from digital_development_team.runner.stages.escalation import (
        answer_entry,
        read_escalation,
        resolve,
        write_resolved,
    )

    svc = ctx.svc
    issue = state.issue_id
    esc = read_escalation(svc.store, issue)
    root = _root(state, ctx)
    if esc is None or root is None:
        return False
    patch = patch_of(recommended_option(esc))
    if patch is None:
        return False
    try:
        spec = svc.store.read(issue, SLOT).content
    except StoreError:
        return False
    cfg = ctx.config.policy.spec_amend
    used = int(state.facts.get("spec_amend_auto", 0))
    if not auto_approvable(patch, spec, cfg, sensitive_globs=ctx.config.sensitive_paths.globs(), used=used):
        return False
    now = ctx.clock.now()
    res_amend = amend_spec(
        svc.store,
        svc.sink,
        issue,
        patch,
        by="system:auto",
        reason=str(esc.get("question") or "agent escalation")[:300],
        root=root,
        guard=ctx.config.policy.command_guard,
        max_files=cfg.max_allowed_files,
        at=now.isoformat(),
        stage=stage,
        auto=True,
    )
    if not res_amend.ok:
        return False
    note = "auto-approved spec change: allowed_files += " + ", ".join(res_amend.added)
    res = resolve(esc, "approve", note, stage=stage.value)
    write_resolved(svc.store, issue, esc, res, by="system", at=now, note=note, is_timeout=False)
    answers = list(state.facts.get("human_answers") or [])
    answers.append(answer_entry(esc, res, by="system", note=note, wait_sec=0))
    state.merge_facts({"human_answers": answers, "spec_amend_auto": used + 1})
    return True


def apply_chosen_patch(
    state: Any, ctx: Any, esc: Mapping[str, Any], choice: str, *, by: str, stage: Stage | None
) -> AmendResult | None:
    """Người (hoặc timeout theo khuyến nghị) chọn một option có spec_patch → áp (không giới hạn tự duyệt, vẫn kiểm
    an toàn). None = option không có spec_patch."""
    rec = str(esc.get("recommendation") or "")
    cid = rec if choice == "approve" else choice
    opt = next(
        (o for o in esc.get("options", []) or [] if isinstance(o, Mapping) and str(o.get("id")) == cid),
        None,
    )
    patch = patch_of(opt)
    root = _root(state, ctx)
    if patch is None or root is None:
        return None
    return amend_spec(
        ctx.svc.store,
        ctx.svc.sink,
        state.issue_id,
        patch,
        by=by,
        reason=str(esc.get("question") or "")[:300],
        root=root,
        guard=ctx.config.policy.command_guard,
        max_files=ctx.config.policy.spec_amend.max_allowed_files,
        at=ctx.clock.now().isoformat(),
        stage=stage,
    )

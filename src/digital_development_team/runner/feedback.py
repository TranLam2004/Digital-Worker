"""Khối phản hồi retry — harness dựng, 0 LLM.

Mỗi khi transition đưa issue QUAY LẠI IMPLEMENT / SPEC / LOCALIZE, runner gom lý do lần trước bị trả về từ facts và
slot: gate máy fail (test fail, đuôi log), file ngoài allowlist, checklist chặn, phản hồi Verifier, chỉ dẫn rework của
Leader, ghi chú người ở gate (hoặc giải thích khi G1 hết giờ), lý do merge conflict. Agent nhận qua extra
`retry_feedback` của brief. Không có lý do cụ thể → None (không sinh chữ thừa).

Trước đây retry của machine gate chạy với rework=0 nên Dev không nhận được gì; ghi chú Leader và lý do merge
conflict không bao giờ tới Dev → retry lặp lại đúng cách cũ rồi BLOCKED.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from digital_development_team.core.enums import Stage
from digital_development_team.policy.hooks import redact_secrets
from digital_development_team.util.text import truncate

RETRY_TARGETS = frozenset({Stage.IMPLEMENT, Stage.SPEC, Stage.LOCALIZE})
MAX_CHARS = 2000
TAIL_LINES = 40
MAX_FAILED_TESTS = 10
FACT = "retry_feedback"

SlotReader = Callable[[str], Mapping[str, Any] | None]
ArtifactReader = Callable[[str], str | None]


def build_retry_feedback(
    *,
    from_stage: Stage,
    to_stage: Stage,
    facts: Mapping[str, Any],
    read_slot: SlotReader,
    read_artifact: ArtifactReader,
) -> dict[str, Any] | None:
    """{from, to, text, refs} hoặc None khi không có gì cụ thể để nói."""
    if to_stage not in RETRY_TARGETS or to_stage is from_stage:
        return None
    lines: list[str] = []
    refs: list[str] = []
    if from_stage is Stage.MACHINE_GATE:
        _machine_gate(facts, read_artifact, lines, refs)
    elif from_stage in (Stage.VERIFY, Stage.REVIEW):
        _evidence(read_slot, lines)
    elif from_stage is Stage.GATE:
        _decision(read_slot, lines)
        _evidence(read_slot, lines)
    elif from_stage in (Stage.G0_WAIT, Stage.G1_WAIT):
        _gate(facts, lines)
    elif from_stage is Stage.MERGE:
        _merge(facts, lines, refs)
    elif from_stage is Stage.IMPLEMENT:
        _preflight(facts, lines)
    if not lines:
        return None
    text = redact_secrets("\n".join(lines))[0]
    return {"from": from_stage.value, "to": to_stage.value, "text": truncate(text, MAX_CHARS), "refs": refs}


# ───── từng nguồn ─────
def _machine_gate(
    facts: Mapping[str, Any], read_artifact: ArtifactReader, lines: list[str], refs: list[str]
) -> None:
    for f in facts.get("mg_failures") or []:
        if not isinstance(f, Mapping):
            continue
        lines.append(f"- machine gate `{f.get('gate')}` failed: {f.get('summary') or '?'}")
        if f.get("cmd"):
            lines.append(f"  command: {f['cmd']}")
        tests = [t for t in f.get("failed_tests") or [] if isinstance(t, Mapping)]
        for t in tests[:MAX_FAILED_TESTS]:
            msg = str(t.get("message") or "").strip()
            lines.append(f"  FAILED {t.get('name')}" + (f" - {msg[:200]}" if msg else ""))
        if len(tests) > MAX_FAILED_TESTS:
            lines.append(f"  … and {len(tests) - MAX_FAILED_TESTS} more failed tests")
        ref = f.get("output_ref")
        if isinstance(ref, str) and ref:
            refs.append(ref)
            tail = _tail(read_artifact(ref))
            if tail and not tests:  # có danh sách test fail thì đuôi log là thừa
                lines.append(f"  last lines of the output ({ref}):")
                lines += [f"    {ln}" for ln in tail]
    mg = facts.get("machine_gate")
    scope = mg.get("scope_violation") if isinstance(mg, Mapping) else None
    if scope:
        lines.append(
            "- files changed outside spec.allowed_files: "
            + ", ".join(map(str, scope))
            + ". Revert them, or escalate if they are truly needed."
        )
    chk = facts.get("checklist")
    if isinstance(chk, Mapping) and chk.get("blocking_fail"):
        failed = [str(x) for x in chk.get("failed_ids") or []]
        lines.append("- blocking checklist items failed on the diff: " + (", ".join(failed) or "?"))


def _evidence(read_slot: SlotReader, lines: list[str]) -> None:
    ev = read_slot("evidence.json")
    if not ev:
        return
    verdict = ev.get("verdict")
    if verdict == "pass":
        return
    fb = str(ev.get("failure_feedback_for_dev") or "").strip()
    if fb:
        lines.append(f"- Verifier ({verdict}): {fb}")
    for c in ev.get("criteria") or []:
        if isinstance(c, Mapping) and c.get("result") in ("fail", "unclear"):
            lines.append(f"  {c.get('id')} {c.get('result')}: {str(c.get('evidence') or '')[:200]}")
    reg = ev.get("regression")
    if isinstance(reg, Mapping) and reg.get("result") == "fail":
        failed = ", ".join(map(str, (reg.get("failed_tests") or [])[:MAX_FAILED_TESTS]))
        lines.append("- regression failed" + (f": {failed}" if failed else ""))


def _decision(read_slot: SlotReader, lines: list[str]) -> None:
    dec = read_slot("decision.json")
    if not dec:
        return
    instr = str(dec.get("rework_instructions") or "").strip()
    if instr:
        lines.append(f"- Leader ({dec.get('action')}): {instr}")
    elif dec.get("action") in ("rework", "re_localize") and dec.get("rationale"):
        lines.append(f"- Leader ({dec.get('action')}): {str(dec['rationale'])[:400]}")


def _gate(facts: Mapping[str, Any], lines: list[str]) -> None:
    gate = facts.get("gate")
    if not isinstance(gate, Mapping) or gate.get("escalation"):
        return  # câu trả lời escalation đã có trong human_answers
    note = str(gate.get("note") or "").strip()
    if gate.get("choice") == "reject" and note:
        who = "timeout" if gate.get("is_timeout") else gate.get("by") or "human"
        lines.append(f"- Rejected at {gate.get('gate')} by {who}: {note}")


def _merge(facts: Mapping[str, Any], lines: list[str], refs: list[str]) -> None:
    merge = facts.get("merge")
    if not isinstance(merge, Mapping) or not merge.get("conflict"):
        return
    lines.append(f"- merge failed: {merge.get('reason') or 'conflict'}")
    files = merge.get("files") or []
    if files:
        lines.append("  conflicting files: " + ", ".join(map(str, files[:10])))
    prior = merge.get("prior_diff_ref")
    if isinstance(prior, str) and prior:
        refs.append(prior)
        lines.append(
            f"  the worktree was reset onto the new base; your previous diff is at {prior} "
            "(read it with fs.read and re-apply what still applies)"
        )


def _preflight(facts: Mapping[str, Any], lines: list[str]) -> None:
    pf = facts.get("preflight")
    if not isinstance(pf, Mapping) or pf.get("status") == "ok":
        return
    lines.append("- the spec could not be used as written (checked on the base commit before coding):")
    lines += [f"  {p}" for p in pf.get("problems") or []]
    lines.append("  Fix test_command / regression_command / allowed_files so they run in this repo.")


def _tail(text: str | None) -> list[str]:
    if not text:
        return []
    out = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return [ln[:200] for ln in out[-TAIL_LINES:]]

"""MERGE — rebase tuần tự · machine gate SAU rebase · ff-only vào default branch · remove worktree (10-harness/07).

Conflict hoặc test đỏ sau rebase → facts merge.conflict=true (policy: IMPLEMENT trên base mới / BLOCKED). KHÔNG để agent giải conflict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import ConfigError, DDTError, IsolationError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import StageResult
from digital_development_team.isolation.worktree import REPO_LOCK
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.machine_gate import run_gate_cmd
from digital_development_team.runner.state import IssueState
from digital_development_team.tools.codegraph import is_indexed, sync_index
from digital_development_team.tools.coverage import BASELINE_FILE, write_baseline

_MERGE_LOCK = (
    REPO_LOCK  # merge_serialize: CHỈ MỘT merge/push tại một thời điểm, cùng khoá với worktree add/remove
)


class MergeStage:
    stage = Stage.MERGE
    uses_llm = False

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        svc = ctx.svc
        if svc.worktrees is None:
            raise ConfigError("MERGE cần repo git", hint="chạy với --repo")
        issue = state.issue_id
        if not state.worktree or not svc.worktrees.exists(issue):
            raise ConfigError(
                "MERGE: không có worktree của issue", hint="IMPLEMENT chưa chạy hoặc worktree đã bị xoá"
            )
        wt = Path(state.worktree)
        git = svc.worktrees.git
        branch = state.branch or svc.worktrees.branch(issue)
        default = svc.worktrees.default_branch()
        facts: dict[str, Any] = {}
        with _MERGE_LOCK:
            from digital_development_team.runner.match import require_active

            ctx.cancel.raise_if_cancelled()
            require_active(svc, issue)
            try:
                if git.is_dirty(cwd=wt):
                    git.commit(cwd=wt, message=f"ddt({issue}): wip trước merge")
                head_before = git.head(cwd=wt)
                ok, files = git.rebase(cwd=wt, onto=default)
            except IsolationError as e:
                return self._conflict(state, ctx, facts, f"rebase lỗi: {e.message}", [])
            if not ok:
                self._reset_onto_base(state, ctx, wt, default, facts)
                return self._conflict(state, ctx, facts, "conflict khi rebase lên base mới", files)
            if ctx.config.policy.machine_gate_after_rebase and git.head(cwd=wt) == head_before:
                # rebase không đổi gì (main chưa đi tiếp) → đúng cây MACHINE_GATE vừa kiểm: chạy lại chỉ tốn thời gian
                facts["merge_gate"] = {
                    "status": "skipped",
                    "summary": "rebase no-op — cây đã qua MACHINE_GATE",
                }
            elif ctx.config.policy.machine_gate_after_rebase:
                gate = self._post_rebase_gate(state, ctx, wt)
                facts["merge_gate"] = gate
                if gate.get("status") == "fail":
                    return self._conflict(
                        state, ctx, facts, f"test đỏ sau rebase: {gate.get('summary', '')}", []
                    )
            contest_gate = self._contest_gate(state, ctx, wt)
            if contest_gate is not None:
                facts["contest_gate"] = contest_gate
                if contest_gate["status"] != "pass":
                    return self._conflict(state, ctx, facts, str(contest_gate["summary"]), [])
            try:
                ctx.cancel.raise_if_cancelled()
                require_active(svc, issue)
                sha = git.merge_ff_only(branch)
            except IsolationError as e:
                return self._conflict(state, ctx, facts, f"ff-only thất bại: {e.message}", [])
            if contest_gate is not None:
                from digital_development_team.runner.match import write_match

                write_match(svc, issue, {"verification": {**contest_gate, "git_revision": sha}})
            facts.update(self._push(state, ctx, default))
        svc.sink.emit(
            EventDraft(
                issue_id=issue,
                actor="runner",
                event=EventType.MERGED,
                stage=self.stage,
                data={
                    "sha": sha[:12],
                    "branch": branch,
                    "into": default,
                    "files": len(
                        git.changed_files(cwd=git.root, base=state.base_commit) if state.base_commit else []
                    ),
                },
            )
        )
        try:
            svc.worktrees.remove(issue, force=True)
            svc.sink.emit(
                EventDraft(
                    issue_id=issue,
                    actor="runner",
                    event=EventType.WORKTREE_REMOVED,
                    stage=self.stage,
                    data={"path": state.worktree},
                )
            )
        except IsolationError as e:  # giữ worktree, không fail merge
            facts["worktree_kept"] = e.message
        state.worktree = None
        facts["merge"] = {
            "conflict": False,
            "commit": sha[:12],
            "sha": sha,
            "into": default,
        }  # sha đầy đủ: gitRevision
        if svc.sandbox is not None and svc.sandbox_cfg is not None and is_indexed(git.root):
            facts["codegraph_sync"] = sync_index(svc.sandbox, svc.sandbox_cfg, git.root, ctx.cancel)
        self._seed_baseline(state, ctx, sha, facts)
        if facts.get("push", {}).get("ok", True):  # nộp bài: Grader lấy đúng commit đã push
            from digital_development_team.runner.match import submit

            try:
                sub = submit(svc, issue, sha)
            except DDTError as e:
                sub = {"status": "DEFERRED", "error": e.message}
            if sub is not None:
                facts["submission"] = {k: sub.get(k) for k in ("status", "submission_id", "error")}
        total = (state.facts.get("machine_gate") or {}).get("coverage_total")
        if isinstance(total, int | float):  # baseline coverage = main sau merge (tools/coverage.py)
            write_baseline(ctx.svc.layout.memory_dir / BASELINE_FILE, float(total), sha)
        return StageResult(stage=self.stage, facts=facts)

    @staticmethod
    def _contest_gate(state: IssueState, ctx: StageContext, wt: Path) -> dict[str, Any] | None:
        from digital_development_team.runner.contest_validation import evidence_problems
        from digital_development_team.runner.match import contest_item, read_match
        from digital_development_team.util.hashing import sha256_json

        if contest_item(ctx.svc, state.issue_id) is None:
            return None
        try:
            spec = ctx.svc.store.read(state.issue_id, "spec.yaml").content
            evidence = ctx.svc.store.read(state.issue_id, "evidence.json").content
        except StoreError:
            return {"status": "fail", "summary": "Missing contest spec or evidence"}
        problems = evidence_problems(read_match(ctx.svc, state.issue_id), spec, evidence)
        if problems:
            return {"status": "fail", "summary": "; ".join(problems)[:600]}
        commands = [
            str(ac["test_command"]) for ac in spec.get("acceptance_criteria", []) if ac.get("mvs_ids")
        ]
        commands += [str(spec.get("test_command") or ""), str(spec.get("regression_command") or "")]
        if not all(commands):
            return {"status": "fail", "summary": "Contest requires test and regression commands"}
        checks = []
        for command in dict.fromkeys(commands):
            check = run_gate_cmd(ctx, wt, command, "contest-final")
            parsed = check.get("parsed") or {}
            if "passed" in parsed and not parsed.get("passed"):
                check = {**check, "status": "fail", "summary": "No passing tests were executed"}
            checks.append({"command": command, **check})
            if check["status"] != "pass":
                return {"status": "fail", "summary": check["summary"], "checks": checks}
        if ctx.svc.worktrees is not None and ctx.svc.worktrees.git.is_dirty(cwd=wt):
            return {"status": "fail", "summary": "Test commands changed the commit being verified"}
        return {
            "status": "pass",
            "summary": "MVS and regression passed on final tree",
            "checks": checks,
            "spec_hash": sha256_json(spec),
            "evidence_hash": sha256_json(evidence),
        }

    @staticmethod
    def _push(state: IssueState, ctx: StageContext, default: str) -> dict[str, Any]:
        """Item thi đấu → push main lên remote đội (trong merge lock: các lần push nối tiếp nhau)."""
        from digital_development_team.gamemaster.fake import FakeGameMaster
        from digital_development_team.runner.match import contest_item, require_active

        remote = ctx.config.policy.gamemaster.push_remote
        svc = ctx.svc
        if svc.worktrees is None or contest_item(svc, state.issue_id) is None:
            return {}
        git = svc.worktrees.git
        if not remote or not git.has_remote(remote):
            allowed = (
                isinstance(svc.gamemaster, FakeGameMaster)
                and ctx.config.policy.gamemaster.allow_unpushed_fake
            )
            return {"push": {"ok": allowed, "error": f"không có remote {remote}"}}
        try:
            require_active(svc, state.issue_id)
            git.push_branch(remote, default)
            git.verify_pushed(remote, default, git.head())
        except DDTError as e:
            return {"push": {"ok": False, "error": e.message[:300]}}
        return {"push": {"ok": True, "remote": remote}}

    @staticmethod
    def _seed_baseline(state: IssueState, ctx: StageContext, sha: str, facts: dict[str, Any]) -> None:
        """Main mới = đúng cây vừa qua regression → issue kế dùng lại baseline (0 lỗi sẵn), khỏi chạy lại.
        Chỉ nạp khi chắc chắn sạch: regression pass và base cũ không có test đỏ sẵn."""
        from digital_development_team.runner.stages.preflight import FACT, baseline_cache_put

        try:
            spec = ctx.svc.store.read(state.issue_id, "spec.yaml").content
        except StoreError:
            return
        cmd = str(spec.get("regression_command") or "").strip()
        pf = state.facts.get(FACT) or {}
        mg = state.facts.get("machine_gate") or {}
        post = facts.get("merge_gate") or {}
        clean = not (pf.get("baseline_failures") if isinstance(pf, dict) else None)
        passed = post.get("status") == "pass" or (
            post.get("status") == "skipped" and mg.get("regression") == "pass"
        )
        if cmd and clean and passed:
            baseline_cache_put(ctx.svc.layout, sha, cmd, [])

    def _reset_onto_base(
        self, state: IssueState, ctx: StageContext, wt: Path, default: str, facts: dict[str, Any]
    ) -> None:
        """Conflict thật (rebase đã abort): lưu diff cũ thành artifact, đưa worktree lên base mới để lần
        IMPLEMENT kế làm lại ở đó (trước đây Dev sửa tiếp trên base cũ → MERGE conflict lại → BLOCKED)."""
        svc = ctx.svc
        if svc.worktrees is None or not state.base_commit:
            return
        try:
            prior = svc.worktrees.git.diff_worktree(cwd=wt, base=state.base_commit)
            ref = (
                svc.artifacts.put(state.issue_id, prior, ext="diff", label="prior-attempt")
                if svc.artifacts is not None and prior.strip()
                else None
            )
            new_base, backup = svc.worktrees.reset_onto(
                state.issue_id, default, backup_suffix=f"attempt{state.attempt}"
            )
        except IsolationError as e:
            facts["merge_reset_error"] = e.message
            return
        state.base_commit = new_base
        facts["merge_reset"] = {"base": new_base[:12], "backup_branch": backup, "prior_diff_ref": ref}

    def _conflict(
        self, state: IssueState, ctx: StageContext, facts: dict[str, Any], reason: str, files: list[str]
    ) -> StageResult:
        ctx.svc.sink.emit(
            EventDraft(
                issue_id=state.issue_id,
                actor="runner",
                event=EventType.MERGE_CONFLICT,
                stage=self.stage,
                data={"reason": reason, "files": files[:10]},
            )
        )
        raw = facts.get("merge_reset")
        reset: dict[str, Any] = raw if isinstance(raw, dict) else {}
        facts["merge"] = {
            "conflict": True,
            "reason": reason,
            "files": files,
            "commit": None,
            **({"prior_diff_ref": reset["prior_diff_ref"]} if reset.get("prior_diff_ref") else {}),
        }
        return StageResult(stage=self.stage, facts=facts)

    def _post_rebase_gate(self, state: IssueState, ctx: StageContext, wt: Path) -> dict[str, Any]:
        try:
            spec = ctx.svc.store.read(state.issue_id, "spec.yaml").content
        except StoreError:
            return {"status": "skipped", "summary": "không có spec"}
        cmd = str(spec.get("regression_command") or spec.get("test_command") or "")
        if not cmd:
            return {"status": "skipped", "summary": "spec không có test_command"}
        return run_gate_cmd(ctx, wt, cmd, "post-rebase")

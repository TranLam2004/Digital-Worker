"""VERIFY ∥ REVIEW — ThreadPoolExecutor, mỗi nhánh một CancelToken con.
VERIFY xong với verdict != pass → hủy REVIEW (policy `cancel: REVIEW`) để không tốn token vô ích."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace

from digital_development_team.core.enums import Stage
from digital_development_team.core.errors import Cancelled
from digital_development_team.core.models import BudgetUsage, StageResult
from digital_development_team.runner.context import StageContext
from digital_development_team.runner.stages.base import StageHandler
from digital_development_team.runner.state import IssueState


class ParallelStages:
    uses_llm = True

    def __init__(self, handlers: list[StageHandler], *, cancel_on_fail: Stage | None = Stage.REVIEW) -> None:
        if not handlers:
            raise ValueError("ParallelStages cần ≥1 handler")
        self._handlers = handlers
        self._cancel_on_fail = cancel_on_fail
        self.stage = handlers[0].stage

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        tokens = {h.stage: ctx.cancel.child() for h in self._handlers}
        facts: dict[str, object] = {}
        outcomes: dict[str, object] = {}
        usage = BudgetUsage()
        with ThreadPoolExecutor(max_workers=len(self._handlers), thread_name_prefix="ddt-par") as pool:
            futures = {
                pool.submit(h.run, state, replace(ctx, cancel=tokens[h.stage])): h.stage
                for h in self._handlers
            }
            pending = set(futures)
            while pending:
                done, pending = wait(pending, return_when="FIRST_COMPLETED")
                for f in done:
                    st = futures[f]
                    try:
                        r = f.result()
                    except Cancelled:
                        facts[f"{st.value.lower()}_cancelled"] = True
                        continue
                    out = r.facts.get("outcome")
                    if isinstance(out, dict):
                        outcomes[st.value] = out
                    facts.update({k: v for k, v in r.facts.items() if k != "outcome"})
                    usage = usage + r.usage
                    if st is Stage.VERIFY and self._cancel_on_fail in tokens:
                        ev = r.facts.get("evidence")
                        verdict = ev.get("verdict") if isinstance(ev, dict) else None
                        if verdict not in (None, "pass"):
                            tokens[self._cancel_on_fail].cancel("VERIFY fail → hủy REVIEW")
        # outcome của stage chính (VERIFY: bảng transition của nhóm là của nó); trước đây nhánh xong sau ghi đè
        if outcomes:
            facts["outcomes"] = outcomes
            facts["outcome"] = outcomes.get(self.stage.value) or next(iter(outcomes.values()))
        return StageResult(stage=self.stage, facts=facts, usage=usage)

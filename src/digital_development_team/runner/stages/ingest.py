"""INGEST — 0 LLM. item.json phải có (ingest CLI ghi, B6). Stub mode: tạo item mẫu nếu thiếu."""

from __future__ import annotations

from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.errors import StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import StageResult
from digital_development_team.runner.context import RUNNER_ACTOR, StageContext
from digital_development_team.runner.stages.base import result
from digital_development_team.runner.state import IssueState


class IngestStage:
    stage = Stage.INGEST
    uses_llm = False

    def run(self, state: IssueState, ctx: StageContext) -> StageResult:
        store = ctx.svc.store
        if not store.exists(state.issue_id, "item.json"):
            if not ctx.stub:
                raise StoreError(
                    f"{state.issue_id}: chưa có item.json", hint="ddt ingest <backlog.csv|json> trước khi run"
                )
            from digital_development_team.runner.stages.stub_data import stub_item

            store.write(state.issue_id, "item.json", stub_item(state.issue_id, ctx.clock), actor=RUNNER_ACTOR)
        item = store.read(state.issue_id, "item.json").content
        ctx.svc.sink.emit(
            EventDraft(
                issue_id=state.issue_id,
                actor="runner",
                event=EventType.INGESTED,
                stage=self.stage,
                data={
                    "source": item.get("source"),
                    "source_id": item.get("source_id"),
                    "title": str(item.get("title", ""))[:120],
                },
            )
        )
        from digital_development_team.runner.match import start_working

        m = start_working(ctx.svc, state.issue_id)  # item thi đấu: báo Game Master, nhận MVS + template sớm
        return result(
            self.stage,
            **(
                {
                    "match": {
                        "item": m.get("item_id"),
                        "started": m.get("started"),
                        "mvs": len(m.get("mvs") or []),
                    }
                }
                if m
                else {}
            ),
            item={
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "labels": list(item.get("labels", [])),
            },
        )

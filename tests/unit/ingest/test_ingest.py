from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from digital_development_team.app import Runtime
from digital_development_team.cli.main import app
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.ingest.base import normalize_priority, split_list
from digital_development_team.ingest.csv_source import CsvSource
from digital_development_team.ingest.json_source import JsonSource
from tests.conftest import TESTDATA

BACKLOGS = TESTDATA / "backlogs"


def test_helpers() -> None:
    assert (
        normalize_priority("p1") == "P1"
        and normalize_priority("high") == "P1"
        and normalize_priority("2") == "P2"
    )
    assert normalize_priority("") is None and normalize_priority("weird") is None
    assert split_list("a; b,c") == ("a", "b", "c") and split_list(["x", " "]) == ("x",)


def test_csv_source_parses_calc() -> None:
    items = list(CsvSource(BACKLOGS / "calc.csv").items())
    assert [i.source_id for i in items] == ["1", "2", "3"]
    assert (
        items[0].labels == ("bug",)
        and items[0].priority_hint == "P2"
        and "ZeroDivisionError" in items[0].body
    )
    assert items[1].labels == ("change_request", "calc") and items[1].blocked_by == ("1",)
    assert items[2].priority_hint == "P3"


def test_json_source_github_shape() -> None:
    items = list(JsonSource(BACKLOGS / "github.json").items())
    assert (
        items[0].source_id == "42"
        and items[0].labels == ("bug", "api")
        and items[0].raw["html_url"].endswith("/42")
    )
    assert items[1].labels == ("security",) and items[1].priority_hint == "P1"


def test_ingest_file_dedupe_blocked_by_and_queue(runtime: Runtime) -> None:
    q = Queue(runtime.db, runtime.clock)
    rep = ingest_file(BACKLOGS / "calc.csv", store=runtime.store, queue=q, clock=runtime.clock)
    assert rep.created == ["DDT-0001", "DDT-0002", "DDT-0003"] and rep.errors == []
    item2 = runtime.store.read("DDT-0002", "item.json").content
    assert item2["blocked_by"] == ["DDT-0001"] and item2["source"] == "csv" and item2["source_id"] == "2"
    assert runtime.registry.is_valid("item", item2)
    # ingest lại → trùng toàn bộ
    rep2 = ingest_file(BACKLOGS / "calc.csv", store=runtime.store, queue=q, clock=runtime.clock)
    assert rep2.created == [] and rep2.skipped == ["1", "2", "3"]
    # nguồn khác → id tiếp nối
    rep3 = ingest_file(BACKLOGS / "github.json", store=runtime.store, queue=q, clock=runtime.clock)
    assert rep3.created == ["DDT-0004", "DDT-0005"]
    # ưu tiên: security P1 (5) trước P2 (20) trước P3 (30)
    order = [r.issue_id for r in q.list("queued")]
    assert order[0] == "DDT-0005" and order.index("DDT-0001") < order.index("DDT-0003")
    nxt = q.next()
    assert nxt is not None and nxt.issue_id == "DDT-0005"
    # blocked_by chưa done → DDT-0002 bị bỏ qua
    for i in ("DDT-0005", "DDT-0004", "DDT-0001"):
        q.set_status(i, "running")
    nxt = q.next()
    assert nxt is not None and nxt.issue_id == "DDT-0003"
    q.set_status("DDT-0001", "done")
    q.set_status("DDT-0003", "done")
    nxt = q.next()
    assert nxt is not None and nxt.issue_id == "DDT-0002"
    assert q.counts() == {"running": 2, "done": 2, "queued": 1}


def test_ingest_injection_body_kept_verbatim(runtime: Runtime) -> None:
    q = Queue(runtime.db, runtime.clock)
    rep = ingest_file(
        BACKLOGS / "injection.csv", store=runtime.store, queue=q, clock=runtime.clock, source_name="jira"
    )
    assert rep.created == ["DDT-0001", "DDT-0002"]
    item = runtime.store.read("DDT-0001", "item.json").content
    assert (
        "Ignore all previous instructions" in item["body"] and item["source"] == "jira"
    )  # dữ liệu, không phải chỉ thị
    # ingest giữ nguyên văn cả thẻ đóng giả; brief mới vô hiệu hoá nó (tests/unit/agents/test_brief.py)
    assert "</untrusted_input>" in runtime.store.read("DDT-0002", "item.json").content["body"]


def test_cli_ingest_queue_and_run_next(tmp_home: Path) -> None:
    cli = CliRunner()
    base = ["--home", str(tmp_home)]
    r = cli.invoke(app, [*base, "ingest", str(BACKLOGS / "calc.csv")])
    assert r.exit_code == 0 and "+3 mới" in r.output, r.output
    r = cli.invoke(app, [*base, "queue"])
    assert r.exit_code == 0 and "DDT-0001" in r.output and "next" in r.output
    r = cli.invoke(app, [*base, "run", "--next", "--stub-stages"])
    assert r.exit_code == 0 and "DDT-0001" in r.output and "done" in r.output, r.output
    r = cli.invoke(app, [*base, "queue", "--status", "done"])
    assert "DDT-0001" in r.output
    # DDT-0002 blocked_by DDT-0001 (đã done) → next là DDT-0002? ưu tiên P3 cả hai: DDT-0002 enqueued trước
    r = cli.invoke(app, [*base, "run", "--next", "--stub-stages"])
    assert r.exit_code == 0 and "DDT-0002" in r.output, r.output
    r = cli.invoke(app, [*base, "ingest", str(BACKLOGS / "nope.txt")])
    assert r.exit_code != 0


def test_mixed_backlog_covers_every_new_task_shape(runtime: Runtime) -> None:
    """testdata/backlogs/mixed.csv (kiểm chứng sống Phase 1–11): refactor · docs · test · spike · CR · quá lớn · lệnh test sai."""
    rep = ingest_file(
        TESTDATA / "backlogs" / "mixed.csv",
        store=runtime.store,
        queue=Queue(runtime.db, runtime.clock),
        clock=runtime.clock,
    )
    assert len(rep.created) == 7 and not rep.errors
    labels = {runtime.store.read(i, "item.json").content["labels"][0] for i in rep.created}
    assert {"refactor", "docs", "test", "spike", "change_request", "bug"} <= labels

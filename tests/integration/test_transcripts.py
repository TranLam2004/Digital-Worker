"""Transcript store: AgentLoop ghi work/<id>/transcripts/<STAGE>.<attempt>.jsonl (brief · turn · tool · outcome), đã redact;
policy.transcripts=false → không ghi."""

from __future__ import annotations

from pathlib import Path

from digital_development_team.app import build_runtime
from digital_development_team.config.loader import load_config
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import Stage
from digital_development_team.eventlog.transcript import TranscriptWriter, list_transcripts, read_transcript
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.runner import Runner, default_handlers
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0001"
GOLDEN = TESTDATA / "golden" / "calc_mean"


def test_transcript_writer_redacts_and_truncates(tmp_home: Path, fake_clock: FakeClock) -> None:
    from digital_development_team.policy.hooks import redact_secrets
    from digital_development_team.storage.layout import HomeLayout

    tw = TranscriptWriter.for_run(
        HomeLayout(tmp_home), ISSUE, "IMPLEMENT", 2, redact=lambda s: redact_secrets(s)[0], clock=fake_clock
    )
    tw.write("brief", text="ANTHROPIC key sk-ant-abcdefghijklmnopqrstuvwxyz123456 trong issue")
    tw.write("tool_result", turn=1, name="fs_read", status="ok", content="x" * 5000)
    tw.write("outcome", result="artifact", uri="blackboard://DDT-0001/patch/@1", reason=None)
    entries = read_transcript(tw.path)
    assert [e["kind"] for e in entries] == ["brief", "tool_result", "outcome"]
    assert "[REDACTED:anthropic]" in entries[0]["text"] and "sk-ant-" not in entries[0]["text"]
    assert len(entries[1]["content"]) == 2000 and entries[1]["content_truncated"] == 5000
    infos = list_transcripts(HomeLayout(tmp_home), ISSUE)
    assert len(infos) == 1 and infos[0].stage == "IMPLEMENT" and infos[0].attempt == 2
    # resume: nối tiếp + note
    tw2 = TranscriptWriter(tw.path, clock=fake_clock)
    tw2.write("harness", turn=2, text="tiếp")
    kinds = [e["kind"] for e in read_transcript(tw.path)]
    assert kinds[-2:] == ["note", "harness"]


def test_agent_loop_writes_transcript(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    _, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN)
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    rep = Runner(rt.services(), handlers=default_handlers(stub=False)).run(
        ISSUE, cancel=CancelToken(), until=Stage.LOCALIZE
    )
    assert rep.stage is Stage.LOCALIZE
    infos = {(t.stage, t.attempt) for t in list_transcripts(rt.layout, ISSUE)}
    assert infos == {("TRIAGE", 1), ("LOCALIZE", 1)}
    triage = read_transcript(rt.layout.transcripts_dir(ISSUE) / "TRIAGE.1.jsonl")
    kinds = [e["kind"] for e in triage]
    assert kinds[0] == "system" and kinds[1] == "brief" and kinds[-1] == "outcome"
    assert triage[0]["role"] == "intake" and triage[0]["transport"] == "api"
    assert "<untrusted_input" in triage[1]["text"] and "ZeroDivisionError" in triage[1]["text"]
    assistant = [e for e in triage if e["kind"] == "assistant"]
    assert len(assistant) == 1 and assistant[0]["turn"] == 1 and assistant[0]["usage"]["tokens_in"] > 0
    assert triage[-1]["kind"] == "outcome" and triage[-1]["uri"] == f"blackboard://{ISSUE}/intake.yaml@1"
    loc = read_transcript(rt.layout.transcripts_dir(ISSUE) / "LOCALIZE.1.jsonl")
    tools = [e for e in loc if e["kind"] == "tool_result"]
    assert [t["name"] for t in tools][:3] == ["fs_search", "fs_list", "fs_read"] and all(
        t["status"] == "ok" for t in tools[:3]
    )
    assert tools[-1]["name"] == "blackboard_write"

    rt.close()


def test_transcripts_disabled_by_policy(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    from dataclasses import replace

    _, repo = make_repo(tmp_path)
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=GOLDEN)
    rt = replace(rt, config=load_config(flags=["--no-transcripts"]))
    assert rt.config.policy.transcripts is False
    ingest_file(
        TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
    )
    Runner(rt.services(), handlers=default_handlers(stub=False)).run(
        ISSUE, cancel=CancelToken(), until=Stage.TRIAGE
    )
    assert list_transcripts(rt.layout, ISSUE) == []
    rt.close()

"""Question fast path với agent thật (mock provider): Intake → Dev ở hồ sơ ANSWER → answer.yaml → DONE.

Chốt: Dev chỉ thấy câu hỏi Intake đã viết lại (không có item thô), chỉ có tool đọc (ghi file bị từ chối),
không tạo worktree, không chạy LOCALIZE…MERGE.
"""

from __future__ import annotations

import json
from pathlib import Path

from digital_development_team.app import build_runtime
from digital_development_team.core.context import CancelToken, FakeClock
from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.llm import ChatRequest, ChatResponse, ProviderCaps, Usage
from digital_development_team.eventlog.reader import read_events
from digital_development_team.ingest import Queue, ingest_file
from digital_development_team.llm.claude_cli_provider import CliAgentResult, TurnInfo
from digital_development_team.llm.mock import MockProvider
from digital_development_team.runner import Runner, default_handlers
from tests.conftest import TESTDATA
from tests.unit.isolation.test_git import make_repo

ISSUE = "DDT-0003"
QUESTION = "Vì sao mean() trả về float kể cả khi mọi phần tử đầu vào là số nguyên?"


def _golden(d: Path) -> Path:
    d.mkdir()
    intake = {
        "issue_id": ISSUE,
        "type": "question",
        "severity": "P3",
        "complexity": "trivial",
        "risk_tier": "T0",
        "route": "fast",
        "confidence": 0.9,
        "affected_area": "calc",
        "entry_hints": ["mean", "float"],
        "rationale": "Người hỏi muốn hiểu hành vi, không yêu cầu sửa.",
        "question": QUESTION,
    }
    answer = {
        "issue_id": ISSUE,
        "answer": "mean() chia tổng cho len(xs) bằng toán tử /, là phép chia thực trong Python 3, nên luôn ra float.",
        "references": ["calc.py#mean"],
        "confidence": 0.85,
    }
    (d / "intake.json").write_text(
        json.dumps({"role": "intake", "steps": [{"text": json.dumps(intake, ensure_ascii=False)}]}),
        encoding="utf-8",
    )
    dev_steps = [
        {
            "text": "Thử ghi rồi đọc code",
            "tool_calls": [
                {"name": "fs_write", "input": {"path": "calc.py", "content": "x = 1\n"}},
                {"name": "fs_read", "input": {"path": "calc.py"}},
            ],
            "stop": "tool_use",
            "expect_in_prompt": ["Question (restated by Intake", QUESTION, "## OUTPUT CONTRACT"],
        },
        {"text": json.dumps(answer, ensure_ascii=False), "stop": "end_turn"},
    ]
    (d / "dev.json").write_text(json.dumps({"role": "dev", "steps": dev_steps}), encoding="utf-8")
    return d


def test_question_answered_read_only(tmp_home: Path, tmp_path: Path, fake_clock: FakeClock) -> None:
    _, repo = make_repo(tmp_path)
    before = (repo / "calc.py").read_text(encoding="utf-8")
    rt = build_runtime(
        tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=_golden(tmp_path / "golden")
    )
    try:
        ingest_file(
            TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
        )
        rep = Runner(rt.services(), handlers=default_handlers(stub=False)).run(ISSUE, cancel=CancelToken())
        assert rep.status == "done", rep
        ans = rt.store.read(ISSUE, "answer.yaml")
        assert ans.ref.written_by == "dev" and "phép chia thực" in ans.content["answer"]
        assert (repo / "calc.py").read_text(encoding="utf-8") == before  # không đổi gì trong repo
        evs = read_events(rt.layout, ISSUE)
        denied = [e for e in evs if e.event is EventType.TOOL_DENIED]
        assert [e.tool.name for e in denied if e.tool] == ["fs.write"]  # ngoài hồ sơ ANSWER
        ok = [e.tool.name for e in evs if e.event is EventType.TOOL_CALL and e.tool and e.tool.status == "ok"]
        assert "fs.read" in ok
        stages = {e.stage.value for e in evs if e.event is EventType.STAGE_ENTER and e.stage}
        assert stages == {"INGEST", "TRIAGE", "ANSWER"}
        # worktree chỉ để chạy lệnh chỉ đọc khi điều tra; ANSWER xong là xoá, repo không đổi
        created = [e for e in evs if e.event is EventType.WORKTREE_CREATED]
        removed = [e for e in evs if e.event is EventType.WORKTREE_REMOVED]
        assert len(created) == len(removed) == 1 and not (rt.layout.worktree_dir(ISSUE)).exists()
        transcript = (rt.layout.work_dir / ISSUE / "transcripts" / "ANSWER.1.jsonl").read_text(
            encoding="utf-8"
        )
        assert QUESTION in transcript and "<untrusted_input" not in transcript  # không có item thô
    finally:
        rt.close()


class _FakeCli:
    """Như ClaudeCliProvider: chat = một call (Intake) · run_agent = loop trong CLI, tool qua MCP (Dev ở ANSWER)."""

    name = "mock"
    caps = ProviderCaps(agent_loop=True)

    def __init__(self, chat: MockProvider, final_text: str) -> None:
        self._chat, self._final = chat, final_text
        self.sessions: list[object] = []

    def chat(self, req: ChatRequest, *, cancel: CancelToken) -> ChatResponse:
        return self._chat.chat(req, cancel=cancel)

    def run_agent(self, *, session, system, brief, model, effort, cancel, on_turn, timeout_s=None):  # type: ignore[no-untyped-def]
        self.sessions.append(session)
        usage = Usage(tokens_in=900, tokens_out=120)
        on_turn(
            TurnInfo(
                index=0,
                text=self._final,
                tool_calls=("mcp__ddt__fs_read",),
                usage=usage,
                stop_reason="end_turn",
                model=model,
            )
        )
        return CliAgentResult(final_text=self._final, turns=1, usage=usage, model=model)


def test_question_via_cli_transport_uses_agent_loop_with_read_tools(
    tmp_home: Path, tmp_path: Path, fake_clock: FakeClock
) -> None:
    """`claude -p` một call bỏ hết tool → Dev ở ANSWER phải đi agent loop CLI (đọc được code); JSON lượt cuối → answer.yaml."""
    _, repo = make_repo(tmp_path)
    golden = _golden(tmp_path / "golden")
    rt = build_runtime(tmp_home, clock=fake_clock, repo=repo, provider="mock", golden_dir=golden)
    try:
        answer = {
            "issue_id": ISSUE,
            "answer": "mean() dùng phép chia thực / nên luôn ra float.",
            "references": ["calc.py#mean"],
            "confidence": 0.8,
        }
        chat = MockProvider(echo=False)
        chat.load_dir(golden)
        fake = _FakeCli(chat, json.dumps(answer, ensure_ascii=False))
        rt.models.register_provider("mock", fake)
        ingest_file(
            TESTDATA / "backlogs" / "calc.csv", store=rt.store, queue=Queue(rt.db, rt.clock), clock=rt.clock
        )
        rep = Runner(rt.services(), handlers=default_handlers(stub=False)).run(ISSUE, cancel=CancelToken())
        assert rep.status == "done", rep
        assert len(fake.sessions) == 1  # chỉ Dev ở ANSWER đi agent loop; Intake vẫn một call
        sess = fake.sessions[0]
        tools = set(sess.tools)  # type: ignore[attr-defined]
        assert sess.stage == Stage.ANSWER.value  # type: ignore[attr-defined]
        assert {"fs.read", "fs.search", "exec.run", "docs.lookup", "web.fetch", "skill.search"} <= tools
        assert not tools & {"fs.write", "fs.edit", "test.run", "git.commit", "blackboard.write"}  # chỉ đọc
        ans = rt.store.read(ISSUE, "answer.yaml")
        assert ans.ref.written_by == "dev" and ans.content["references"] == ["calc.py#mean"]
    finally:
        rt.close()

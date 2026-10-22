"""Retro mỗi 10 issue (0 LLM): 3 tốt · 3 tệ · 3 hành động có tick; chỉ số canary / lift / gate ở /api/assess."""

from __future__ import annotations

from digital_development_team.app import Runtime
from digital_development_team.core.context import FakeClock
from digital_development_team.reporting.retro import build_retro, maybe_write_retro, read_actions, set_action

COLS = (
    "issue_id, role, agent_version, issue_type, module, tier, outcome, passed, reason, tokens, cost_usd, calls,"
    " model, seq_no, finished_at, learned_skills, memory_refs, no_learn"
)


def _seed(rt: Runtime) -> None:
    """20 issue: 10 đầu Dev hay fail ở module auth; 10 sau Dev tốt hơn nhưng analyst kém, có lý do từ chối lặp lại."""
    with rt.db.transaction() as c:
        for i in range(1, 21):
            issue = f"DDT-{i:04d}"
            module = "auth" if i % 2 else "calc"
            dev_pass = (module == "calc") if i <= 10 else (i % 4 != 1)
            rows = [
                ("dev", int(dev_pass), 20000 if i <= 10 else 12000, 1 if i > 15 else 0, 1 if i > 12 else 0),
                ("analyst", int(i <= 10 or i % 3 == 0), 3000, 0, 0),
                ("verifier", 1, 5000, 0, 0),
            ]
            for role, passed, tok, learned, mem in rows:
                c.execute(
                    f"INSERT INTO role_outcomes({COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        issue,
                        role,
                        1,
                        "bug",
                        module,
                        "T1",
                        "done",
                        passed,
                        "",
                        tok,
                        0.0,
                        1,
                        "m",
                        i,
                        "t",
                        learned,
                        mem,
                        0,
                    ),
                )
            if i in (12, 14, 16):
                c.execute(
                    "INSERT INTO gate_requests(id, issue_id, gate, attempt, tier, summary, opened_at, status, choice,"
                    " decided_by, note, resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{issue}:G1:1",
                        issue,
                        "G1",
                        1,
                        "T1",
                        "{}",
                        "t",
                        "resolved",
                        "reject",
                        "human:P1",
                        "Thiếu test: AC2 không có test",
                        "t",
                    ),
                )
        c.execute(
            "INSERT INTO canary_results(issue_id, attempt, mutation, file, line, caught, verdict, at) VALUES(?,?,?,?,?,?,?,?)",
            ("DDT-0013", 1, "off_by_one", "calc.py", 9, 0, "pass", "t"),
        )


def test_build_retro_finds_good_bad_actions(runtime: Runtime) -> None:
    _seed(runtime)
    r = build_retro(runtime.db, upto=20, every=10)
    assert r["range"] == "11-20" and r["issues"] == 10
    assert len(r["good"]) <= 3 and len(r["bad"]) == 3 and len(r["actions"]) == 3
    joined_bad = " ".join(r["bad"])
    assert "Thiếu test" in joined_bad or "analyst pass thấp" in joined_bad or "canary" in joined_bad
    assert any("Token mỗi issue giảm" in g for g in r["good"])
    assert all(a["done"] is False and a["id"].startswith("A") for a in r["actions"])


def test_retro_file_and_ticks(runtime: Runtime, fake_clock: FakeClock) -> None:
    _seed(runtime)
    assert maybe_write_retro(runtime.layout, runtime.db, 19, now=fake_clock.now()) is None
    p = maybe_write_retro(runtime.layout, runtime.db, 20, now=fake_clock.now())
    assert p is not None and p.name == "retro_020.md" and "## 3 hành động" in p.read_text(encoding="utf-8")
    items = set_action(runtime.layout, p.name, "A1", done=True, by="human:P3", at=fake_clock.now())
    assert items[0]["done"] is True and items[0]["by"] == "human:P3"
    assert "- [x] **A1**" in p.read_text(encoding="utf-8") and read_actions(runtime.layout, p.name)[0]["done"]

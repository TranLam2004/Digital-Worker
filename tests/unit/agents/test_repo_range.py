"""Đoạn code theo dòng của plan.candidates cho brief Dev (B3): đúng khoảng ±pad, có số dòng, file ngắn → cả file."""

from __future__ import annotations

from pathlib import Path

from digital_development_team.agents.repo_context import FsRepoContext, parse_span, pick_top_ranges


def test_read_range_window_with_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("\n".join(f"line{i}" for i in range(1, 501)), encoding="utf-8")
    ctx = FsRepoContext(tmp_path)
    text = ctx.read_range("big.py", "200-210", pad=5, max_lines=160)
    assert text is not None
    assert text.startswith("… (194 lines above)") and "  195  line195" in text and "  215  line215" in text
    assert "line194" not in text and "line216" not in text and text.endswith("(285 more lines)")
    (tmp_path / "small.py").write_text("a\nb\n", encoding="utf-8")
    assert ctx.read_range("small.py", "999", max_lines=160) == "    1  a\n    2  b"
    assert ctx.read_range("missing.py", "1") is None


def test_pick_top_ranges_and_parse_span() -> None:
    cands = [
        {"files": [{"path": "a.py", "lines": "10-20"}, {"path": "b.py"}]},
        {"files": [{"path": "a.py", "lines": "1-2"}, {"path": "c.py", "lines": "5"}]},
    ]
    assert pick_top_ranges(cands) == [("a.py", "10-20"), ("b.py", None), ("c.py", "5")]
    assert parse_span("end of file") is None and parse_span("3") == (3, 3)

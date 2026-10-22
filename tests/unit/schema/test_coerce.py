"""coerce_to_schema: chỉ sửa giá trị chắc chắn bị schema từ chối, không đổi nghĩa — và FileStore áp trước validate."""

from __future__ import annotations

from digital_development_team.schema import SchemaRegistry
from digital_development_team.schema.coerce import coerce_to_schema


def test_enum_with_annotation_is_coerced() -> None:
    reg = SchemaRegistry()
    dec = {
        "summary_for_human": {"regression": "pass (13 passed full run)", "security": "PASS", "top_risk": "x"}
    }
    out, notes = coerce_to_schema(reg.schema("decision"), dec)
    s = out["summary_for_human"]
    assert s["regression"] == "pass" and s["security"] == "pass" and s["top_risk"] == "x"
    assert len(notes) == 2 and all("(enum)" in n for n in notes)


def test_enum_ambiguous_or_unknown_is_left_for_validation() -> None:
    reg = SchemaRegistry()
    out, notes = coerce_to_schema(
        reg.schema("decision"), {"summary_for_human": {"regression": "unknown yet"}}
    )
    assert out["summary_for_human"]["regression"] == "unknown yet" and notes == []


def test_maxlength_truncates_and_object_wrap() -> None:
    reg = SchemaRegistry()
    plan = {"candidates": [{"rank": 1, "files": ["shop/cart.py"], "confidence": 0.8, "reason": "r" * 230}]}
    out, notes = coerce_to_schema(reg.schema("plan"), plan)
    c = out["candidates"][0]
    assert c["files"] == [{"path": "shop/cart.py"}] and len(c["reason"]) == 200 and c["reason"].endswith("…")
    assert any("maxLength" in n for n in notes) and any("object" in n for n in notes)


def test_valid_data_untouched() -> None:
    reg = SchemaRegistry()
    plan = {"candidates": [{"rank": 1, "files": [{"path": "a.py"}], "confidence": 0.5, "reason": "ok"}]}
    out, notes = coerce_to_schema(reg.schema("plan"), plan)
    assert out == plan and notes == []


def test_optional_field_with_bad_pattern_is_dropped() -> None:
    reg = SchemaRegistry()
    plan = {
        "candidates": [
            {
                "rank": 1,
                "files": [{"path": "a.py", "lines": "end of file"}, {"path": "b.py", "lines": "3-9"}],
                "confidence": 0.5,
                "reason": "ok",
            }
        ]
    }
    out, notes = coerce_to_schema(reg.schema("plan"), plan)
    files = out["candidates"][0]["files"]
    assert files[0] == {"path": "a.py"} and files[1] == {"path": "b.py", "lines": "3-9"}
    assert any("dropped" in n for n in notes)
    # field required sai pattern → giữ nguyên để validate báo lỗi (không tự sửa nghĩa)
    out2, _ = coerce_to_schema(reg.schema("plan"), {"issue_id": "X-1"})
    assert out2["issue_id"] == "X-1"

"""Contract: View ⊆ JSON Schema · EventType/Stage == enum trong event.schema · Event model dump pass schema."""

from __future__ import annotations

import typing
from datetime import UTC, datetime
from enum import StrEnum

import pytest
from pydantic import BaseModel

from digital_development_team.core.enums import EventType, Stage
from digital_development_team.core.events import Event, EventDraft
from digital_development_team.schema import SchemaRegistry
from digital_development_team.schema.views import VIEWS


@pytest.fixture(scope="module")
def reg() -> SchemaRegistry:
    return SchemaRegistry()


def test_stage_enum_matches_event_schema(reg: SchemaRegistry) -> None:
    assert reg.schema("event")["properties"]["stage"]["enum"] == [s.value for s in Stage]


def test_event_type_enum_matches_event_schema(reg: SchemaRegistry) -> None:
    assert reg.schema("event")["properties"]["event"]["enum"] == [e.value for e in EventType]


def test_event_model_dump_passes_schema(reg: SchemaRegistry) -> None:
    ev = Event.from_draft(
        EventDraft(
            issue_id="DDT-0001",
            actor="runner",
            event=EventType.STAGE_ENTER,
            stage=Stage.TRIAGE,
            data={"x": 1},
        ),
        seq=0,
        ts=datetime(2026, 10, 3, tzinfo=UTC),
    )
    import json

    assert reg.is_valid("event", json.loads(ev.to_line()))


def _enum_values(ann: object) -> set[str] | None:
    """Literal[...] hoặc StrEnum → tập giá trị; None nếu không phải."""
    origin = typing.get_origin(ann)
    if origin is typing.Literal:
        return {str(a) for a in typing.get_args(ann)}
    if origin is typing.Union or str(origin) == "types.UnionType":
        vals: set[str] = set()
        for a in typing.get_args(ann):
            if a is type(None):
                continue
            v = _enum_values(a)
            if v is None:
                return None
            vals |= v
        return vals or None
    if isinstance(ann, type) and issubclass(ann, StrEnum):
        return {e.value for e in ann}
    return None


@pytest.mark.parametrize("sid", sorted(VIEWS))
def test_view_subset_of_schema(reg: SchemaRegistry, sid: str) -> None:
    view = VIEWS[sid]
    schema = reg.schema(sid)
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    for name, field in view.model_fields.items():
        key = field.alias or name
        assert key in props, f"{sid}: View.{name} không có trong schema"
        if field.is_required():
            assert key in required, f"{sid}: View.{name} bắt buộc nhưng schema không required"
        view_vals = _enum_values(field.annotation)
        schema_enum = props[key].get("enum")
        if view_vals is not None and schema_enum is not None:
            assert view_vals <= set(schema_enum), (
                f"{sid}.{name}: View có giá trị ngoài schema enum {view_vals - set(schema_enum)}"
            )


def test_nested_view_models_are_pydantic() -> None:
    for v in VIEWS.values():
        assert issubclass(v, BaseModel)

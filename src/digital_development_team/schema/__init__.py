"""JSON Schema artifact = nguồn sự thật (xem README › Quyết định kiến trúc). Pydantic View chỉ để đọc.

reg = SchemaRegistry(resolver)
reg.raise_if_invalid("spec", data)      # SchemaError với ≤5 vi phạm
reg.for_slot("spec.yaml")               # dict schema
render_contract(reg.schema("spec"), "spec.yaml")   # khối OUTPUT CONTRACT cho brief
"""

from __future__ import annotations

from digital_development_team.schema.contract import render_contract
from digital_development_team.schema.registry import SLOT_SCHEMA, SchemaRegistry, schema_id_for_slot

__all__ = ["SLOT_SCHEMA", "SchemaRegistry", "render_contract", "schema_id_for_slot"]

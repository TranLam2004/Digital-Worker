"""Nạp mọi schemas/*.schema.{json,yaml}, Draft7Validator + FORMAT_CHECKER. Validate trước khi ghi blackboard."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft7Validator, FormatChecker
from jsonschema.exceptions import SchemaError as JsonSchemaMetaError

from digital_development_team.config.resources import AssetResolver, build_resolver
from digital_development_team.core.errors import ConfigError, SchemaError, SchemaViolation
from digital_development_team.util import yamlio

SLOT_SCHEMA: dict[str, str] = {
    "item.json": "item",
    "match.json": "match",
    "intake.yaml": "intake",
    "plan.yaml": "plan",
    "spec.yaml": "spec",
    "evidence.json": "evidence",
    "sec_report.json": "sec_report",
    "decision.json": "decision",
    "escalation.yaml": "escalation",
    "clarify.yaml": "clarify",
    "answer.yaml": "answer",
    "blocked_report.json": "blocked_report",
    "learn_report.json": "learn_report",
    "progress.yaml": "progress",
    "events.jsonl": "event",
}
"""patch/ không có schema — dùng PatchBundle + diff_sanity hook."""


def schema_id_for_slot(slot: str) -> str | None:
    return SLOT_SCHEMA.get(slot)


class SchemaRegistry:
    def __init__(self, resolver: AssetResolver | None = None) -> None:
        self._resolver = resolver or build_resolver()
        self._schemas: dict[str, dict[str, Any]] = {}
        self._validators: dict[str, Draft7Validator] = {}
        self._sources: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        hits = [
            h
            for h in self._resolver.list("schemas", ".json") + self._resolver.list("schemas", ".yaml")
            if ".schema." in h.rel
        ]
        if not hits:
            raise ConfigError("không tìm thấy schema nào trong schemas/")
        for hit in hits:
            text = hit.read_text()
            data = json.loads(text) if hit.rel.endswith(".json") else yamlio.loads(text, source=hit.rel)
            if not isinstance(data, dict):
                raise ConfigError(f"{hit.rel}: schema phải là object")
            sid = (
                str(data.get("$id", "")).removeprefix("ddt/")
                or hit.rel.rsplit("/", 1)[-1].split(".schema.")[0]
            )
            self._schemas[sid] = data
            self._sources[sid] = hit.layer
            self._validators[sid] = Draft7Validator(data, format_checker=FormatChecker())

    # ───── truy vấn ─────
    def ids(self) -> list[str]:
        return sorted(self._schemas)

    def schema(self, schema_id: str) -> dict[str, Any]:
        try:
            return self._schemas[schema_id]
        except KeyError:
            raise ConfigError(f"schema lạ: {schema_id!r}", hint=f"có: {self.ids()}") from None

    def for_slot(self, slot: str) -> dict[str, Any] | None:
        sid = schema_id_for_slot(slot)
        return self.schema(sid) if sid else None

    def source(self, schema_id: str) -> str:
        return self._sources[schema_id]

    # ───── validate ─────
    def violations(self, schema_id: str, data: Mapping[str, Any]) -> list[SchemaViolation]:
        v = self._validators.get(schema_id)
        if v is None:
            raise ConfigError(f"schema lạ: {schema_id!r}")
        out: list[SchemaViolation] = []
        for err in sorted(v.iter_errors(data), key=lambda e: list(e.absolute_path)):
            path = ".".join(str(p) for p in err.absolute_path) or "<root>"
            out.append(SchemaViolation(path, err.message))
        return out

    def is_valid(self, schema_id: str, data: Mapping[str, Any]) -> bool:
        return not self.violations(schema_id, data)

    def raise_if_invalid(self, schema_id: str, data: Mapping[str, Any]) -> None:
        errs = self.violations(schema_id, data)
        if errs:
            raise SchemaError(f"{schema_id}: {len(errs)} vi phạm", errs, schema=schema_id)

    def validate_slot(self, slot: str, data: Mapping[str, Any]) -> None:
        sid = schema_id_for_slot(slot)
        if sid is None:
            return  # patch/ — PatchBundle tự validate
        self.raise_if_invalid(sid, data)

    # ───── kiểm toàn bộ (ddt schema check) ─────
    def check_all(self) -> list[tuple[str, str]]:
        """Trả [(schema_id, lỗi)] — rỗng = OK. Kiểm: hợp lệ draft-07, examples pass, slot map đủ."""
        problems: list[tuple[str, str]] = []
        for sid, data in self._schemas.items():
            try:
                Draft7Validator.check_schema(data)
            except JsonSchemaMetaError as e:
                problems.append((sid, f"không hợp lệ draft-07: {e.message}"))
                continue
            if "$id" not in data:
                problems.append((sid, "thiếu $id"))
            for i, ex in enumerate(data.get("examples", [])):
                errs = self.violations(sid, ex)
                if errs:
                    problems.append((sid, f"examples[{i}] fail: {errs[0]}"))
        for slot, sid in SLOT_SCHEMA.items():
            if sid not in self._schemas:
                problems.append((sid, f"slot {slot} trỏ schema không tồn tại"))
        return problems

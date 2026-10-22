"""FileStore — Store thật: fs là sự thật, SQLite là index, mọi write qua schema.

write() KHÔNG: gọi LLM · sửa file cũ · ghi khi schema fail.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from digital_development_team.blackboard.slots import SlotAcl, SlotKind, slot_spec
from digital_development_team.core.context import Actor, Clock, SystemClock
from digital_development_team.core.enums import EventType
from digital_development_team.core.errors import SchemaError, SchemaViolation, SlotPermissionError, StoreError
from digital_development_team.core.events import EventDraft
from digital_development_team.core.models import Artifact, ArtifactRef, PatchBundle
from digital_development_team.core.protocols import EventSink
from digital_development_team.schema.coerce import coerce_to_schema
from digital_development_team.schema.registry import SchemaRegistry
from digital_development_team.storage.db import Database
from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util import jsonio, yamlio
from digital_development_team.util.fsio import read_text, write_text_atomic
from digital_development_team.util.hashing import sha256_text

log = logging.getLogger(__name__)

_MAX_ERRORS_TO_AGENT = 5


def actor_label(actor: Actor) -> str:
    return f"{actor.role}:{actor.sub_id}" if actor.sub_id else actor.role


def _dumps(slot: str, data: Mapping[str, Any]) -> str:
    if slot.endswith(".yaml"):
        return yamlio.dumps(dict(data))
    return jsonio.dumps(data)


def _loads(path: Path) -> dict[str, Any]:
    text = read_text(path)
    data = yamlio.loads(text, source=str(path)) if path.suffix == ".yaml" else jsonio.loads(text)
    if not isinstance(data, dict):
        raise StoreError(f"{path}: artifact phải là object")
    return data


_ABORT_WORDS = re.compile(r"\b(abort|skip|stop|block)|dừng|bỏ qua|hủy|huỷ", re.IGNORECASE)


def escalation_problems(form: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Luật cứng JSON Schema không diễn đạt được: người không bao giờ thấy form kém (04-escalation)."""
    out: list[tuple[str, str]] = []
    opts = [o for o in form.get("options", []) if isinstance(o, Mapping)]
    ids = [str(o.get("id")) for o in opts]
    if len(set(ids)) != len(ids):
        out.append(("options", f"id option bị trùng: {ids}"))
    if str(form.get("recommendation")) not in ids:
        out.append(("recommendation", f"phải là một trong {ids}, không được để người tự nghĩ"))
    if form.get("type") == "capability" and not any(
        o.get("route") == "abort" or _ABORT_WORDS.search(str(o.get("action", ""))) for o in opts
    ):
        out.append(("options", "type capability phải có một option dừng hoặc bỏ qua (route: abort)"))
    return out


class FileStore:
    def __init__(
        self,
        layout: HomeLayout,
        db: Database,
        registry: SchemaRegistry,
        sink: EventSink,
        *,
        acl: SlotAcl | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._layout = layout
        self._db = db
        self._registry = registry
        self._sink = sink
        self._acl = acl or SlotAcl()
        self._clock = clock or SystemClock()

    @property
    def acl(self) -> SlotAcl:
        return self._acl

    @property
    def layout(self) -> HomeLayout:
        return self._layout

    # ───── đọc ─────
    def latest(self, issue: str, slot: str) -> ArtifactRef | None:
        row = self._db.one(
            "SELECT * FROM slots WHERE issue_id=? AND slot=? ORDER BY version DESC LIMIT 1", (issue, slot)
        )
        return self._row_to_ref(row) if row else None

    def list(self, issue: str) -> Sequence[ArtifactRef]:
        rows = self._db.all("SELECT * FROM slots WHERE issue_id=? ORDER BY slot, version", (issue,))
        return [self._row_to_ref(r) for r in rows]

    def exists(self, issue: str, slot: str) -> bool:
        return self.latest(issue, slot) is not None

    def read(self, issue: str, slot: str, *, version: int | None = None) -> Artifact:
        spec = slot_spec(slot)
        if spec.kind is SlotKind.LOG:
            raise StoreError("events.jsonl đọc qua eventlog.reader, không qua store")
        if version is None:
            ref = self.latest(issue, slot)
        else:
            row = self._db.one(
                "SELECT * FROM slots WHERE issue_id=? AND slot=? AND version=?", (issue, slot, version)
            )
            ref = self._row_to_ref(row) if row else None
        if ref is None:
            raise StoreError(
                f"{issue}/{slot}" + (f"@{version}" if version else "") + " chưa có",
                hint="nếu file có trên đĩa mà index thiếu → ddt debug reindex",
            )
        path = self._layout.abs(ref.path)
        if not path.is_file():
            raise StoreError(f"index trỏ tới file không tồn tại: {ref.path}", hint="ddt debug reindex")
        content = _loads(path)
        actual = sha256_text(read_text(path))
        if actual != ref.sha256:
            raise StoreError(f"{ref.path} bị sửa sau khi ghi (sha lệch)", expected=ref.sha256, actual=actual)
        return Artifact(ref=ref, content=content)

    def read_as(self, issue: str, slot: str, actor: Actor, *, version: int | None = None) -> Artifact:
        """Đọc có ACL + projection — tools/blackboard dùng. Harness dùng read()."""
        if not self._acl.can_read(actor.role, slot):
            raise SlotPermissionError(
                f"{actor_label(actor)} không được đọc {slot}", role=actor.role, slot=slot
            )
        art = self.read(issue, slot, version=version)
        projected = self._acl.project(actor.role, slot, art.content)
        return art if projected == art.content else Artifact(ref=art.ref, content=projected)

    # ───── ghi ─────
    def write(
        self,
        issue: str,
        slot: str,
        content: Mapping[str, Any] | PatchBundle,
        *,
        actor: Actor,
    ) -> ArtifactRef:
        spec = slot_spec(slot)
        if spec.kind is SlotKind.LOG:
            raise StoreError("events.jsonl ghi qua EventSink.emit, không qua store")
        who = actor_label(actor)
        if not self._acl.can_write(actor.role, slot):
            raise SlotPermissionError(f"{who} không được ghi {slot}", role=actor.role, slot=slot)

        data = self._normalize(slot, spec.kind, content)
        coerced: list[str] = []
        schema = self._registry.for_slot(slot) if spec.kind is SlotKind.FILE else None
        if schema is not None:
            if "issue_id" in (schema.get("properties") or {}) and data.get("issue_id") != issue:
                if "issue_id" in data:  # agent ghi sai id (live: "DDT-0000") — harness biết id thật
                    coerced.append(f"issue_id: {str(data['issue_id'])[:20]!r} → {issue!r} (harness)")
                data = {**data, "issue_id": issue}
            data, more = coerce_to_schema(schema, data)  # chỉ sửa giá trị chắc chắn bị từ chối
            coerced += more
        self._validate(issue, slot, who, data)

        cur = self.latest(issue, slot)
        version = cur.version + 1 if cur else 1
        if spec.kind is SlotKind.FILE and slot == "item.json" and cur is not None:
            raise StoreError("item.json bất biến — không ghi lại", issue=issue)

        text = _dumps(slot, data)
        vpath = self._layout.slot_versioned_path(issue, slot, version)
        if vpath.exists():
            raise StoreError(
                f"file version đã tồn tại: {vpath.name}", hint="index lệch fs → ddt debug reindex"
            )
        write_text_atomic(vpath, text)
        self._write_latest_copy(issue, slot, spec.kind, data, text)

        ref = ArtifactRef(
            issue_id=issue,
            slot=slot,
            version=version,
            path=self._layout.rel(vpath),
            sha256=sha256_text(text),
            written_by=who,
            written_at=self._clock.now(),
        )
        with self._db.transaction() as c:
            c.execute(
                "INSERT INTO slots(issue_id, slot, version, path, sha256, written_by, written_at, schema_ok) "
                "VALUES(?,?,?,?,?,?,?,1)",
                (issue, slot, version, ref.path, ref.sha256, who, ref.written_at.isoformat()),
            )
        if cur is not None:
            self._emit(
                issue, who, EventType.SLOT_VERSIONED, {"slot": slot, "from": cur.version, "to": version}
            )
        self._emit(
            issue,
            who,
            EventType.SLOT_WRITTEN,
            {
                "slot": slot,
                "version": version,
                "path": ref.path,
                **({"coerced": coerced[:10]} if coerced else {}),
            },
            artifact_ref=ref.uri,
            hash=ref.sha256,
        )
        return ref

    # ───── nội bộ ─────
    def _normalize(
        self, slot: str, kind: SlotKind, content: Mapping[str, Any] | PatchBundle
    ) -> dict[str, Any]:
        if kind is SlotKind.DIR:
            try:
                bundle = (
                    content if isinstance(content, PatchBundle) else PatchBundle.model_validate(dict(content))
                )
            except ValidationError as e:
                errs = [
                    SchemaViolation(".".join(str(p) for p in x["loc"]) or "<root>", x["msg"])
                    for x in e.errors()
                ]
                raise SchemaError(f"{slot}: {len(errs)} vi phạm", errs, slot=slot) from None
            if bundle.is_empty:
                raise SchemaError(
                    f"{slot}: patch rỗng", [SchemaViolation("diff", "diff rỗng hoặc không đụng file nào")]
                )
            return bundle.model_dump(mode="json")
        if isinstance(content, PatchBundle):
            raise StoreError(f"{slot} không nhận PatchBundle")
        return dict(content)

    def _validate(self, issue: str, slot: str, who: str, data: Mapping[str, Any]) -> None:
        if slot == "patch/":
            return  # PatchBundle đã validate ở _normalize; diff_sanity ở policy hook
        try:
            self._registry.validate_slot(slot, data)
            if slot == "escalation.yaml":
                problems = escalation_problems(data)
                if problems:
                    raise SchemaError(
                        "escalation.yaml chưa đạt form (04-escalation)",
                        [SchemaViolation(path, msg) for path, msg in problems],
                    )
        except SchemaError as e:
            self._emit(
                issue, who, EventType.SLOT_REJECTED, {"slot": slot, "errors": e.brief(_MAX_ERRORS_TO_AGENT)}
            )
            raise

    def _write_latest_copy(
        self, issue: str, slot: str, kind: SlotKind, data: Mapping[str, Any], text: str
    ) -> None:
        latest = self._layout.slot_latest_path(issue, slot)
        if kind is SlotKind.FILE:
            write_text_atomic(latest, text, fsync=False)
            return
        # patch/ → materialize cho người đọc: diff.patch · files_touched.json · impl_log.md
        latest.mkdir(parents=True, exist_ok=True)
        write_text_atomic(latest / "diff.patch", str(data.get("diff", "")), fsync=False)
        write_text_atomic(
            latest / "files_touched.json", jsonio.dumps(list(data.get("files_touched", []))), fsync=False
        )
        write_text_atomic(latest / "impl_log.md", str(data.get("impl_log", "")), fsync=False)

    def _emit(self, issue: str, who: str, event: EventType, data: dict[str, Any], **kw: Any) -> None:
        self._sink.emit(EventDraft(issue_id=issue, actor=who, event=event, data=data, **kw))

    @staticmethod
    def _row_to_ref(row: Any) -> ArtifactRef:
        return ArtifactRef(
            issue_id=row["issue_id"],
            slot=row["slot"],
            version=row["version"],
            path=row["path"],
            sha256=row["sha256"],
            written_by=row["written_by"],
            written_at=row["written_at"],
        )

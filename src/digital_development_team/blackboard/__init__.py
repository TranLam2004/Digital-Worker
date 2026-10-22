"""Blackboard — kênh giao tiếp DUY NHẤT giữa agent (Design §6.2, §8).

store.write(issue, "spec.yaml", data, actor=Actor(role="analyst"))
  → ACL (owner) → validate schema → version mới bất biến + copy latest → index SQLite → emit slot_*
store.read(issue, "spec.yaml")                 bản mới nhất
store.read(issue, "spec.yaml", version=1)      bản cũ
acl.project(role, slot, content)               cắt field theo ma trận (Dev chỉ thấy failure_feedback_for_dev)
"""

from __future__ import annotations

from digital_development_team.blackboard.checkpoints import BoardRow, CheckpointStore
from digital_development_team.blackboard.reindex import ReindexReport, reindex
from digital_development_team.blackboard.slots import SLOTS, SlotAcl, SlotKind, SlotSpec
from digital_development_team.blackboard.store import FileStore

__all__ = [
    "SLOTS",
    "BoardRow",
    "CheckpointStore",
    "FileStore",
    "ReindexReport",
    "SlotAcl",
    "SlotKind",
    "SlotSpec",
    "reindex",
]

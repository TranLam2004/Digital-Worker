"""Bản đồ thư mục DDT home — mọi đường dẫn đi qua đây, không ai tự ghép chuỗi.

<home>/                       (= <repo-parent>/<repo>.ddt/, ngoài repo đích)
  ddt.db                      SQLite: slots index · event_seq · locks · gate_requests · queue · board · episodes
  work/<issue>/
    item.json  intake.yaml  spec.yaml …      bản MỚI NHẤT (copy, để người cat)
    spec.v1.yaml  spec.v2.yaml …             bản BẤT BIẾN theo version (index trỏ vào đây)
    patch.v1.json + patch/{diff.patch,files_touched.json,impl_log.md}
    events.jsonl  checkpoint.json  artifacts/<sha>  raw/  subs/
  state/   events.global.jsonl  STOP
  memory/  REPO.md  project_kb/  workers/
  worktrees/<issue>/
  overrides/config/*.yaml       lớp override config (config.resources)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from digital_development_team.config.resources import HOME_OVERRIDES_DIR

GLOBAL_ISSUE = "global"
"""issue_id đặc biệt cho event hệ thống → state/events.global.jsonl."""

_VERSIONED_RE = re.compile(r"^(?P<stem>[a-z][a-z0-9_]*)\.v(?P<version>\d+)\.(?P<ext>yaml|json)$")


def slot_stem_ext(slot: str) -> tuple[str, str]:
    """'spec.yaml' → ('spec','yaml') · 'patch/' → ('patch','json') (slot thư mục lưu PatchBundle dạng json)."""
    if slot.endswith("/"):
        return slot[:-1], "json"
    stem, _, ext = slot.rpartition(".")
    return stem, ext


def parse_versioned_name(filename: str) -> tuple[str, int] | None:
    """'spec.v2.yaml' → ('spec.yaml', 2) · 'patch.v1.json' → ('patch/', 1) · khác → None."""
    m = _VERSIONED_RE.match(filename)
    if not m:
        return None
    stem, version, ext = m["stem"], int(m["version"]), m["ext"]
    slot = "patch/" if stem == "patch" else f"{stem}.{ext}"
    return slot, version


@dataclass(frozen=True, slots=True)
class HomeLayout:
    home: Path

    # ───── gốc ─────
    @property
    def db_path(self) -> Path:
        return self.home / "ddt.db"

    @property
    def work_dir(self) -> Path:
        return self.home / "work"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    @property
    def overrides_dir(self) -> Path:
        return self.home / HOME_OVERRIDES_DIR

    @property
    def stop_file(self) -> Path:
        return self.state_dir / "STOP"

    @property
    def reports_dir(self) -> Path:
        """Báo cáo cho người: session_*.md · handover_*.md · perf_*.md · retro_*.md."""
        return self.home / "reports"

    @property
    def inbox_dir(self) -> Path:
        """Backlog người tải lên từ web, chờ worker nạp (storage/control.py); nạp xong chuyển sang inbox/done/."""
        return self.home / "inbox"

    @property
    def global_events_path(self) -> Path:
        return self.state_dir / "events.global.jsonl"

    # ───── theo issue ─────
    def issue_dir(self, issue: str) -> Path:
        return self.work_dir / issue

    def events_path(self, issue: str) -> Path:
        if issue == GLOBAL_ISSUE:
            return self.global_events_path
        return self.issue_dir(issue) / "events.jsonl"

    def checkpoint_path(self, issue: str) -> Path:
        return self.issue_dir(issue) / "checkpoint.json"

    def artifacts_dir(self, issue: str) -> Path:
        return self.issue_dir(issue) / "artifacts"

    def raw_dir(self, issue: str) -> Path:
        return self.issue_dir(issue) / "raw"

    def subs_dir(self, issue: str) -> Path:
        return self.issue_dir(issue) / "subs"

    def transcripts_dir(self, issue: str) -> Path:
        """Transcript từng lần chạy agent (eventlog/transcript.py): <STAGE>.<attempt>.jsonl."""
        return self.issue_dir(issue) / "transcripts"

    def worktree_dir(self, issue: str) -> Path:
        return self.worktrees_dir / issue

    def slot_versioned_path(self, issue: str, slot: str, version: int) -> Path:
        stem, ext = slot_stem_ext(slot)
        return self.issue_dir(issue) / f"{stem}.v{version}.{ext}"

    def slot_latest_path(self, issue: str, slot: str) -> Path:
        """Bản copy mới nhất, tên gốc. Slot thư mục → thư mục đã materialize."""
        if slot.endswith("/"):
            return self.issue_dir(issue) / slot[:-1]
        return self.issue_dir(issue) / slot

    # ───── tiện ích ─────
    def rel(self, path: Path) -> str:
        """POSIX tương đối home: work/DDT-0001/spec.v2.yaml."""
        return PurePosixPath(path.relative_to(self.home)).as_posix()

    def abs(self, rel: str) -> Path:
        return self.home / Path(rel)

    def issue_ids(self) -> list[str]:
        if not self.work_dir.is_dir():
            return []
        return sorted(p.name for p in self.work_dir.iterdir() if p.is_dir())

    def ensure(self) -> HomeLayout:
        for d in (self.work_dir, self.state_dir, self.memory_dir, self.worktrees_dir, self.overrides_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

"""G3 — kill switch = file state/STOP. Tồn tại = dừng. `ddt stop` / `ddt resume` / `touch`."""

from __future__ import annotations

from datetime import UTC, datetime

from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util.fsio import ENCODING, write_text_atomic


class StopSwitch:
    def __init__(self, layout: HomeLayout) -> None:
        self._path = layout.stop_file

    def is_stopped(self) -> bool:
        return self._path.exists()

    def request(self, *, by: str = "human") -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self._path, f"{by} {datetime.now(UTC).isoformat()}\n", fsync=False)

    def clear(self) -> bool:
        """True nếu có STOP để xoá."""
        if not self._path.exists():
            return False
        self._path.unlink()
        return True

    def info(self) -> str | None:
        if not self._path.exists():
            return None
        return self._path.read_text(encoding=ENCODING).strip() or "?"

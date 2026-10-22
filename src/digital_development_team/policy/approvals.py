"""Lớp 4 — G2: người duyệt từng lời gọi tool phá huỷ (02-gates).

Luồng: policy chặn lời gọi cần G2 và trả `approval_key` → agent ghi escalation destructive_request rồi dừng → harness mở
ticket G2 (không hết giờ) → người chọn cho phép một lần · cho phép cả issue · từ chối → harness ghi quyền vào
`work/<issue>/approvals.json` → agent chạy lại đúng lời gọi đó, policy thấy khoá thì cho qua. Quyền "một lần" bị xoá
ngay sau khi dùng. Không có "cho phép mãi mãi".

Khoá gắn đúng lời gọi: fs.* theo đường dẫn, exec/test theo hash lệnh, tool khác theo hash đối số.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from digital_development_team.storage.layout import HomeLayout
from digital_development_team.util import jsonio
from digital_development_team.util.fsio import write_text_atomic
from digital_development_team.util.hashing import sha256_text

SCOPES = ("once", "issue")


def approval_key(tool: str, args: Mapping[str, Any], rel_path: str | None) -> str:
    if tool.startswith("fs.") and rel_path:
        return f"{tool}:{rel_path}"
    cmd = args.get("cmd")
    if isinstance(cmd, str) and cmd.strip():
        return f"{tool}:cmd:{_short(cmd.strip())}"
    return f"{tool}:args:{_short(jsonio.dumps_canonical(dict(args)))}"


def _short(text: str) -> str:
    return sha256_text(text).rsplit(":", 1)[-1][:16]


class ApprovalStore:
    """Quyền G2 theo issue. Harness (worker) ghi khi người duyệt; dispatcher đọc trước mỗi lời gọi."""

    def __init__(self, layout: HomeLayout) -> None:
        self._layout = layout

    def _path(self, issue: str) -> Any:
        return self._layout.issue_dir(issue) / "approvals.json"

    def load(self, issue: str) -> list[dict[str, Any]]:
        p = self._path(issue)
        if not p.is_file():
            return []
        data = jsonio.loads(p.read_text(encoding="utf-8"))
        return [dict(a) for a in data.get("approvals", []) if isinstance(a, dict)]

    def active(self, issue: str) -> dict[str, str]:
        """{khoá: scope} còn hiệu lực."""
        return {str(a["key"]): str(a["scope"]) for a in self.load(issue) if not a.get("used")}

    def grant(
        self, issue: str, *, key: str, scope: str, by: str, at: datetime, detail: str = ""
    ) -> dict[str, Any]:
        if scope not in SCOPES:
            raise ValueError(f"scope lạ: {scope}")
        items = [a for a in self.load(issue) if a.get("key") != key or a.get("used")]
        entry = {
            "key": key,
            "scope": scope,
            "by": by,
            "at": at.isoformat(),
            "detail": detail[:200],
            "used": False,
        }
        items.append(entry)
        self._save(issue, items)
        return entry

    def consume(self, issue: str, key: str, *, at: datetime) -> None:
        """Quyền "một lần" đã dùng → đánh dấu used (giữ lại để audit)."""
        items = self.load(issue)
        for a in items:
            if a.get("key") == key and a.get("scope") == "once" and not a.get("used"):
                a["used"] = True
                a["used_at"] = at.isoformat()
        self._save(issue, items)

    def _save(self, issue: str, items: list[dict[str, Any]]) -> None:
        p = self._path(issue)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(p, jsonio.dumps({"approvals": items}), fsync=False)


__all__ = ["SCOPES", "ApprovalStore", "approval_key"]

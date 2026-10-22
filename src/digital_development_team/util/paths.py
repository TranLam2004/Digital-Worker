"""Đường dẫn an toàn. Trong repo/config/schema luôn là chuỗi POSIX; chỉ đổi sang Path ở ranh giới fs.

resolve_inside chặn: '..', tuyệt đối, ':' (Alternate Data Stream trên NTFS), tên reserved Windows,
prefix \\\\?\\, symlink/junction trỏ ra ngoài root.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from digital_development_team.core.errors import DDTError

_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')


class UnsafePath(DDTError):
    code = "UNSAFE_PATH"


def to_posix(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def validate_rel(rel: str) -> str:
    """Kiểm chuỗi tương đối POSIX trước khi join. Trả về chuỗi đã chuẩn hóa."""
    if not rel:
        raise UnsafePath("đường dẫn rỗng")
    if "\\" in rel:
        raise UnsafePath("dùng '/' không dùng '\\'", path=rel)
    if rel.startswith("/") or rel.startswith("\\\\?\\"):
        raise UnsafePath("không nhận đường dẫn tuyệt đối", path=rel)
    if ":" in rel:
        raise UnsafePath("':' bị cấm (ổ đĩa hoặc Alternate Data Stream)", path=rel)
    if _BAD_CHARS.search(rel):
        raise UnsafePath("ký tự không hợp lệ", path=rel)
    if "//" in rel or rel.endswith("/"):
        raise UnsafePath("không nhận '//' hay dấu '/' cuối", path=rel)
    parts = PurePosixPath(rel).parts
    for p in parts:
        if p in ("..", "."):
            raise UnsafePath("không nhận '.' hay '..'", path=rel)
        if p.split(".")[0].upper() in _RESERVED:
            raise UnsafePath(f"tên reserved Windows: {p}", path=rel)
    return "/".join(parts)


def resolve_inside(root: Path, rel: str) -> Path:
    """root / rel, bảo đảm kết quả thật (sau symlink) vẫn nằm trong root."""
    clean = validate_rel(rel)
    root_r = root.resolve()
    target = (root_r / PurePosixPath(clean)).resolve()
    if not _is_within(target, root_r):
        raise UnsafePath("đường dẫn thoát khỏi root (symlink/junction?)", path=rel, root=str(root))
    return target


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def rel_posix(root: Path, path: Path) -> str:
    return to_posix(path.resolve().relative_to(root.resolve()))


def normcase(rel: str) -> str:
    """So khớp allowlist không phân biệt hoa thường trên Windows."""
    return os.path.normcase(rel).replace("\\", "/")


def same_path(a: str, b: str) -> bool:
    return normcase(a) == normcase(b)

"""File test hay file nguồn — dùng cho blast radius, impact, tự duyệt spec_patch. Đủ cho Python · JS/TS · Java/Kotlin ·
C# · Go. Tên lớp Java/C# so khớp phân biệt hoa thường (`Latest.cs` không phải test)."""

from __future__ import annotations

import re

_DIRS = ("tests/", "test/", "__tests__/", "spec/", "src/test/", "testdata/")
_NAME = re.compile(r"(^test_.*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$)", re.I)
_CLASS_SUFFIXES = ("Test.java", "Tests.java", "IT.java", "Test.kt", "Tests.kt", "Test.cs", "Tests.cs")


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    low = "/" + p.lower()
    name = p.rsplit("/", 1)[-1]
    if any("/" + d in low for d in _DIRS) or ".tests/" in low or ".test/" in low:
        return True
    return bool(_NAME.search(name)) or name.endswith(_CLASS_SUFFIXES)

"""GlobSet: gitwildmatch (pathspec) + mở rộng brace {a,b}. fnmatch/PurePath.match không hiểu '**' trên 3.12."""

from __future__ import annotations

import re
from collections.abc import Iterable

import pathspec

from digital_development_team.util.paths import normcase

_BRACE = re.compile(r"\{([^{}]*)\}")


def expand_braces(pattern: str) -> list[str]:
    """'{.env,.env.*}' → ['.env', '.env.*']. Lồng một cấp đủ dùng."""
    m = _BRACE.search(pattern)
    if not m:
        return [pattern]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(expand_braces(pattern[: m.start()] + alt + pattern[m.end() :]))
    return out


class GlobSet:
    def __init__(self, patterns: Iterable[str]) -> None:
        expanded: list[str] = []
        for p in patterns:
            expanded.extend(expand_braces(p))
        self.patterns = tuple(expanded)
        self._spec = pathspec.PathSpec.from_lines("gitignore", (normcase(p) for p in expanded))

    def match(self, rel_posix: str) -> bool:
        return self._spec.match_file(normcase(rel_posix))

    def any_match(self, paths: Iterable[str]) -> bool:
        return any(self.match(p) for p in paths)

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def __repr__(self) -> str:
        return f"GlobSet({list(self.patterns)!r})"

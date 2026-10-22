from __future__ import annotations

import sys
from pathlib import Path

import pytest

from digital_development_team.core.errors import ConfigError
from digital_development_team.util import fsio, globs, hashing, paths, text, yamlio


def test_atomic_write_and_eol(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b.txt"
    fsio.write_text_atomic(p, "xin chào\nthế giới\n")
    assert fsio.read_text(p) == "xin chào\nthế giới\n"
    assert not list((tmp_path / "a").glob(".*.tmp"))
    assert fsio.detect_eol("a\r\nb\r\n") == "\r\n"
    assert fsio.detect_eol("a\nb\n") == "\n"
    assert fsio.apply_eol(fsio.normalize_eol("a\r\nb"), "\r\n") == "a\r\nb"


def test_atomic_overwrite(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    fsio.write_text_atomic(p, "1")
    fsio.write_text_atomic(p, "2")
    assert fsio.read_text(p) == "2"


def test_read_retries_transient_windows_sharing_violation(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_text("complete checkpoint", encoding="utf-8")
    original = Path.read_text
    calls = 0

    def temporarily_locked(p: Path, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise PermissionError("file temporarily locked")
        return original(p, **kwargs)

    monkeypatch.setattr(Path, "read_text", temporarily_locked)
    monkeypatch.setattr(fsio.time, "sleep", lambda _: None)
    assert fsio.read_text(path) == "complete checkpoint" and calls == 3


def test_read_permission_failure_has_bounded_retries(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def locked(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise PermissionError("persistent")

    monkeypatch.setattr(Path, "read_text", locked)
    monkeypatch.setattr(fsio.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        fsio.read_text(tmp_path / "checkpoint.json")
    assert calls == 10


def test_concurrent_atomic_writers_never_share_a_temporary_file(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "state.json"
    values = [str(i) * 10000 for i in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: fsio.write_text_atomic(path, value), values))
    assert fsio.read_text(path) in values
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "bad", ["../x", "a/../b", "/abs", "C:/x", "a:stream", "CON", "dir/NUL.txt", "a\\b", "", "a//b"]
)
def test_validate_rel_rejects(bad: str) -> None:
    with pytest.raises(paths.UnsafePath):
        paths.validate_rel(bad)


def test_validate_rel_accepts() -> None:
    assert paths.validate_rel("src/api/users.py") == "src/api/users.py"
    assert paths.validate_rel("./src/x.py".removeprefix("./")) == "src/x.py"


def test_resolve_inside(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    assert paths.resolve_inside(tmp_path, "src/a.py").exists()
    with pytest.raises(paths.UnsafePath):
        paths.resolve_inside(tmp_path, "../outside")


@pytest.mark.skipif(sys.platform != "win32", reason="symlink test đơn giản hóa cho Windows")
def test_normcase_windows() -> None:
    assert paths.same_path("Src/API/Users.py", "src/api/users.py")


def test_globset_brace_and_doublestar() -> None:
    g = globs.GlobSet(["{.env,.env.*}", "**/secrets/**", "src/**/*.py"])
    assert g.match(".env") and g.match(".env.local")
    assert g.match("a/b/secrets/k.pem")
    assert g.match("src/x/y/z.py")
    assert not g.match("README.md")
    assert globs.expand_braces("a{1,2}b{x,y}") == ["a1bx", "a1by", "a2bx", "a2by"]


def test_yaml_duplicate_key_rejected() -> None:
    with pytest.raises(ConfigError, match="trùng key"):
        yamlio.loads("a: 1\na: 2\n")
    assert yamlio.loads("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


def test_hashing_stable() -> None:
    assert hashing.sha256_json({"b": 1, "a": 2}) == hashing.sha256_json({"a": 2, "b": 1})
    assert hashing.sha256_text("x").startswith("sha256:")
    assert len(hashing.short(hashing.sha256_text("x"))) == 8


def test_text_helpers() -> None:
    assert text.estimate_tokens("a" * 400) == 100
    lines = [str(i) for i in range(30)]
    ht = text.head_tail(lines, 3, 2)
    assert ht[0] == "0" and ht[-1] == "29" and "lines omitted" in ht[3]
    assert text.one_line("  a\n b   c ", 5) == "a b c"
    assert text.one_line("  a\n b   c ", 4) == "a b…"

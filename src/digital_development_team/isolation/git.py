"""Git wrapper — mọi lệnh `git -C <cwd> …` qua util.proc (timeout · kill tree · output ra file).

KHÔNG có reset --hard/clean. push_branch chỉ cho harness (MERGE của item thi đấu: nộp bài cần commit đã push);
agent không có tool push. Identity cố định ddt-dev <ddt@local>. Không cd, không shell.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from digital_development_team.core.context import CancelToken
from digital_development_team.core.errors import IsolationError
from digital_development_team.util.fsio import ENCODING
from digital_development_team.util.proc import run_process

_ENV_PASS = ("SYSTEMROOT", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL", "SSH_AUTH_SOCK")
_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


@dataclass(frozen=True, slots=True)
class GitResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def check(self, what: str) -> GitResult:
        if not self.ok:
            raise IsolationError(f"git {what} thất bại (exit {self.exit_code}): {self.stderr.strip()[:300]}")
        return self


class Git:
    def __init__(self, repo_root: Path, *, scratch: Path, git_exe: str = "git") -> None:
        self.root = repo_root
        self._scratch = scratch
        self._git = git_exe

    # ───── chạy ─────
    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout_s: float = 60.0,
        cancel: CancelToken | None = None,
        stdin: str | None = None,
    ) -> GitResult:
        cwd = cwd or self.root
        token = uuid.uuid4().hex[:8]
        out, err = self._scratch / f"git-{token}.out", self._scratch / f"git-{token}.err"
        try:
            res = run_process(
                [self._git, "-C", str(cwd), *args],
                cwd=cwd,
                env=self._env(),
                timeout_s=timeout_s,
                stdout_path=out,
                stderr_path=err,
                cancel=cancel or CancelToken(),
                stdin_text=stdin,
            )
            stdout = out.read_text(encoding=ENCODING, errors="replace") if out.exists() else ""
            stderr = err.read_text(encoding=ENCODING, errors="replace") if err.exists() else ""
        finally:
            for p in (out, err):
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
        if res.timed_out:
            raise IsolationError(f"git {' '.join(args[:2])} timeout {timeout_s}s")
        return GitResult(res.exit_code, stdout, stderr, res.duration_ms)

    @staticmethod
    def _env() -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_PASS}
        env.update(
            {
                "GIT_AUTHOR_NAME": "ddt-dev",
                "GIT_AUTHOR_EMAIL": "ddt@local",
                "GIT_COMMITTER_NAME": "ddt-dev",
                "GIT_COMMITTER_EMAIL": "ddt@local",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "PYTHONUTF8": "1",
                "LC_ALL": "C.UTF-8",
            }
        )
        return env

    def verify_pushed(self, remote: str, branch: str, sha: str) -> None:
        """The team branch must contain the exact commit promised to the grader."""
        result = self.run(["ls-remote", "--exit-code", remote, f"refs/heads/{branch}"]).check("ls-remote")
        fields = result.stdout.split()
        if not fields or not re.fullmatch(r"[0-9a-fA-F]{40}", fields[0]):
            raise IsolationError("Remote branch did not return a full commit SHA")
        if not self.run(["merge-base", "--is-ancestor", sha, fields[0]]).ok:
            raise IsolationError("Submission commit is not reachable from the pushed team branch")

    # ───── repo ─────
    def init(self, *, default_branch: str = "main") -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.run(["init", "-q", "-b", default_branch]).check("init")

    def default_branch(self) -> str:
        r = self.run(["symbolic-ref", "--short", "-q", "HEAD"])
        if r.ok and r.stdout.strip():
            return r.stdout.strip()
        for b in ("main", "master"):
            if self.branch_exists(b):
                return b
        raise IsolationError("không xác định được default branch", hint="repo cần main hoặc master")

    def branch_exists(self, branch: str, *, cwd: Path | None = None) -> bool:
        return self.run(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=cwd).ok

    def head(self, *, cwd: Path | None = None) -> str:
        return self.run(["rev-parse", "HEAD"], cwd=cwd).check("rev-parse").stdout.strip()

    def rev_parse(self, ref: str, *, cwd: Path | None = None) -> str:
        return (
            self.run(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=cwd)
            .check(f"rev-parse {ref}")
            .stdout.strip()
        )

    def merge_base(self, a: str, b: str, *, cwd: Path | None = None) -> str:
        return self.run(["merge-base", a, b], cwd=cwd).check("merge-base").stdout.strip()

    def status_porcelain(self, *, cwd: Path | None = None) -> list[tuple[str, str]]:
        """[(XY, path)] — path POSIX."""
        r = self.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=cwd).check("status")
        out: list[tuple[str, str]] = []
        for entry in r.stdout.split("\0"):
            if len(entry) >= 4:
                out.append((entry[:2], entry[3:]))
        return out

    def visible_files(self, *, cwd: Path | None = None) -> list[str]:
        """File git thấy: đã theo dõi + mới chưa bị .gitignore (bỏ output build: node_modules, target/, bin/…)."""
        r = self.run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=cwd).check(
            "ls-files"
        )
        return sorted({p for p in r.stdout.split("\0") if p})

    def is_dirty(self, *, cwd: Path | None = None) -> bool:
        return bool(self.status_porcelain(cwd=cwd))

    def current_branch(self, *, cwd: Path | None = None) -> str:
        return self.run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).check("branch").stdout.strip()

    # ───── diff / log ─────
    def diff(
        self, *, cwd: Path | None = None, base: str, path: str | None = None, cached: bool = False
    ) -> str:
        args = ["diff", "--no-color", "--no-ext-diff", "--unified=3"]
        if cached:
            args.append("--cached")
        args.append(f"{base}...HEAD" if not cached else base)
        if path:
            args += ["--", path]
        return self.run(args, cwd=cwd).check("diff").stdout

    def diff_worktree(self, *, cwd: Path | None = None, base: str) -> str:
        """Diff base → working tree (gồm cả chưa commit) — Dev chưa commit vẫn thấy được."""
        return self.run(["diff", "--no-color", "--no-ext-diff", base], cwd=cwd).check("diff").stdout

    def numstat(self, *, cwd: Path | None = None, base: str) -> list[tuple[str, int, int]]:
        r = self.run(["diff", "--numstat", f"{base}...HEAD"], cwd=cwd).check("numstat")
        out: list[tuple[str, int, int]] = []
        for line in r.stdout.splitlines():
            m = _NUMSTAT_RE.match(line)
            if m:
                a, d, p = m.groups()
                out.append((p, 0 if a == "-" else int(a), 0 if d == "-" else int(d)))
        return out

    def changed_files(self, *, cwd: Path | None = None, base: str) -> list[str]:
        r = self.run(["diff", "--name-only", f"{base}...HEAD"], cwd=cwd).check("name-only")
        return [x for x in r.stdout.splitlines() if x.strip()]

    def log(
        self, *, cwd: Path | None = None, n: int = 20, path: str | None = None, search: str | None = None
    ) -> list[dict[str, str]]:
        args = ["log", f"-n{n}", "--no-color", "--format=%H%x1f%cI%x1f%an%x1f%s", "--shortstat"]
        if search:
            args.append(f"-S{search}")
        if path:
            args += ["--", path]
        r = self.run(args, cwd=cwd).check("log")
        commits: list[dict[str, str]] = []
        for line in r.stdout.splitlines():
            if "\x1f" in line:
                sha, date, author, msg = line.split("\x1f", 3)
                commits.append(
                    {
                        "sha": sha[:12],
                        "date": date,
                        "author_hash": _hash8(author),
                        "message": msg,
                        "files_changed": "",
                    }
                )
            elif commits and "changed" in line:
                commits[-1]["files_changed"] = line.strip()
        return commits

    # ───── write ─────
    def add_all(self, *, cwd: Path | None = None) -> None:
        self.run(["add", "-A"], cwd=cwd).check("add")

    def commit(self, *, cwd: Path | None = None, message: str, all_: bool = True) -> str | None:
        """sha mới, None nếu không có gì để commit."""
        if all_:
            self.add_all(cwd=cwd)
        if not self.run(["diff", "--cached", "--quiet"], cwd=cwd).ok:
            self.run(["commit", "-q", "--no-verify", "-m", message], cwd=cwd).check("commit")
            return self.head(cwd=cwd)
        return None

    def worktree_add(self, path: Path, *, branch: str, base: str) -> None:
        if self.branch_exists(branch):
            self.run(["worktree", "add", "-q", str(path), branch]).check("worktree add")
        else:
            self.run(["worktree", "add", "-q", "-b", branch, str(path), base]).check("worktree add -b")

    def worktree_remove(self, path: Path, *, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        self.run([*args, str(path)]).check("worktree remove")
        self.run(["worktree", "prune"])

    def worktree_list(self) -> list[str]:
        r = self.run(["worktree", "list", "--porcelain"]).check("worktree list")
        return [line[9:] for line in r.stdout.splitlines() if line.startswith("worktree ")]

    def branch_delete(self, branch: str) -> None:
        self.run(["branch", "-D", "-q", branch])

    def rebase(self, *, cwd: Path, onto: str) -> tuple[bool, list[str]]:
        """(ok, conflict_files). Conflict → abort, trả False."""
        r = self.run([*("rebase", onto)], cwd=cwd, timeout_s=120)
        if r.ok:
            return True, []
        files = [p for xy, p in self.status_porcelain(cwd=cwd) if "U" in xy or xy in ("AA", "DD")]
        self.run(["rebase", "--abort"], cwd=cwd)
        return False, files

    def merge_ff_only(self, branch: str) -> str:
        self.run(["merge", "--ff-only", "-q", branch]).check("merge --ff-only")
        return self.head()

    def branch_force(self, name: str, ref: str, *, cwd: Path | None = None) -> None:
        self.run(["branch", "-f", name, ref], cwd=cwd).check(f"branch -f {name}")

    def reset_hard(self, ref: str, *, cwd: Path) -> None:
        self.run(["reset", "-q", "--hard", ref], cwd=cwd).check(f"reset --hard {ref}")
        self.run(["clean", "-q", "-fd"], cwd=cwd).check("clean")

    def has_remote(self, name: str) -> bool:
        return self.run(["remote", "get-url", name]).ok

    def push_branch(self, remote: str, branch: str, *, timeout_s: float = 120.0) -> None:
        """Push nhánh mặc định lên remote đội (không --force). Chỉ harness gọi, sau merge ff-only."""
        self.run(["push", "-q", remote, f"{branch}:{branch}"], timeout_s=timeout_s).check(
            f"push {remote} {branch}"
        )

    def checkout(self, ref: str, *, cwd: Path | None = None) -> None:
        self.run(["checkout", "-q", ref], cwd=cwd).check("checkout")


def _hash8(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]

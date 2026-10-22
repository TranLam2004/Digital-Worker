"""Kiểm lệnh shell trước khi chạy exec.run / test.run (Lớp 1, 0 LLM).

LocalSandbox không phải ranh giới bảo mật thật (không chặn mạng, không mount chỉ đọc), nên khi chưa có Docker đây là
lớp chặn chính. Không dùng regex trên cả chuỗi vì dễ lách (`git -c x=y push`, `curl''`, `python -c "subprocess…"`)
và dễ chặn nhầm (`rsync` khớp `nc\\s`, file `test_dockerfile.py` khớp `docker`). Thay vào đó: tách lệnh thành đoạn theo
toán tử shell, bỏ lớp bọc (env, timeout, xargs…), rồi xét chương trình đầu mỗi đoạn và đối số của nó.

Danh sách chặn là dữ liệu: `policy.yaml#command_guard` (config/models.py CommandGuardCfg).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from digital_development_team.config.models import CommandGuardCfg
from digital_development_team.util.globs import GlobSet

SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "|&", ";;"})
WRAPPERS = frozenset(
    {"env", "nohup", "time", "timeout", "nice", "command", "builtin", "exec", "xargs", "stdbuf"}
)
PYTHONS = re.compile(r"^(python|python3|python3\.\d+|py|pypy|pypy3)$")
INLINE_FLAGS = {
    "node": ("-e", "--eval", "-p", "--print"),
    "deno": ("eval",),
    "ruby": ("-e",),
    "perl": ("-e", "-E"),
}
SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
BACKSLASH = chr(92)
SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "nul", "NUL", "&1", "&2", "1", "2"})
URL = re.compile(r"(?i)\b(https?|ftp|ssh|git)://")
HOME_REFS = ("$HOME", "${HOME}", "%USERPROFILE%", "$USERPROFILE", "%APPDATA%", "$APPDATA", "%HOMEPATH%")
WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")
READONLY_CODE = re.compile(
    r"open\([^)]*['\"][wax+]|\.write_(text|bytes)\(|\.unlink\(|\.rmdir\(|\.rename\(|\.replace\(|"
    r"shutil\.(move|copy\w*|rmtree)|os\.(remove|unlink|rename|replace|makedirs|mkdir)|fs\.(write|unlink|rm)\w*",
)
RM_RECURSIVE = frozenset({"-r", "-R", "-rf", "-fr", "-Rf", "-fR", "--recursive"})
GIT_BRANCH_WRITE = frozenset(
    {
        "-f",
        "--force",
        "-d",
        "-D",
        "--delete",
        "-m",
        "-M",
        "--move",
        "-c",
        "-C",
        "--copy",
        "-u",
        "--set-upstream-to",
    }
)


@dataclass(frozen=True, slots=True)
class CommandVerdict:
    ok: bool
    rule: str = ""
    reason: str = ""
    hint: str | None = None


OK = CommandVerdict(True)


def _deny(rule: str, reason: str, hint: str | None = None) -> CommandVerdict:
    return CommandVerdict(False, rule, reason, hint)


def tokenize(cmd: str) -> list[str] | None:
    """Tách token như shell. Xuống dòng = hết lệnh. Trả None khi ngoặc kép lệch (không đoán)."""
    text = cmd.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ; ")
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _is_redirect(tok: str) -> bool:
    return bool(tok) and set(tok) <= set("<>&") and ("<" in tok or ">" in tok)


def split_segments(tokens: Sequence[str]) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """[(argv, [(toán tử redirect, đích)])] theo toán tử nối lệnh."""
    out: list[tuple[list[str], list[tuple[str, str]]]] = []
    argv: list[str] = []
    redirects: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in SEPARATORS:
            if argv or redirects:
                out.append((argv, redirects))
            argv, redirects = [], []
        elif _is_redirect(tok):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt and nxt not in SEPARATORS and not _is_redirect(nxt):
                redirects.append((tok, ("&" if tok.endswith("&") else "") + nxt))
                i += 1
        else:
            argv.append(tok)
        i += 1
    if argv or redirects:
        out.append((argv, redirects))
    return out


def prog_name(tok: str) -> str:
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suf in SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def unwrap(argv: list[str]) -> list[str]:
    """Bỏ biến môi trường đầu lệnh và lớp bọc (env, timeout 10, xargs -n 1 …) để lộ chương trình thật."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        name = prog_name(tok)
        if name not in WRAPPERS:
            break
        i += 1
        while i < len(argv) and argv[i].startswith("-"):
            opt = argv[i]
            i += 1
            if name == "xargs" and opt in ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"):
                i += 1
        if name == "timeout" and i < len(argv) and re.match(r"^\d+(\.\d+)?[smhd]?$", argv[i]):
            i += 1
        if name == "env":
            while i < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i]):
                i += 1
    return argv[i:]


def _path_problem(
    tok: str, cfg: CommandGuardCfg, secrets: GlobSet, worktree: str | None
) -> CommandVerdict | None:
    raw = tok.strip().strip("'\"")
    values = [raw] + ([raw.split("=", 1)[1]] if "=" in raw and not raw.startswith("=") else [])
    for v in values:
        if not v:
            continue
        if URL.search(v):
            return _deny(
                "network_url",
                f"địa chỉ mạng trong lệnh: {v[:60]}",
                "Sandbox không có mạng; dùng dữ liệu trong repo",
            )
        if v.startswith("~") or any(h in v for h in HOME_REFS):  # ~ chỉ ở đầu (HEAD~1 không phải home)
            return _deny(
                "outside_worktree", f"đường dẫn ngoài worktree: {v[:60]}", "Chỉ làm việc trong worktree"
            )
        posix = v.replace("\\", "/")
        parts = [p for p in posix.split("/") if p]
        if ".." in parts:
            return _deny(
                "outside_worktree", f"đường dẫn đi ra ngoài: {v[:60]}", "Không dùng .. để ra ngoài worktree"
            )
        if any(secrets.match(p) for p in parts) or (parts and secrets.match("/".join(parts))):
            return _deny("secret_path", f"đụng file bí mật: {v[:60]}", "File bí mật bị chặn với mọi role")
        is_abs = posix.startswith("/") or bool(WIN_ABS.match(v))
        if is_abs and not _allowed_abs(posix, cfg.allow_abs_prefixes, worktree):
            return _deny(
                "outside_worktree",
                f"đường dẫn tuyệt đối ngoài worktree: {v[:60]}",
                "Dùng đường dẫn tương đối trong worktree",
            )
    return None


def _allowed_abs(posix: str, prefixes: Iterable[str], worktree: str | None) -> bool:
    low = posix.lower()
    if any(low == p.lower().rstrip("/") or low.startswith(p.lower()) for p in prefixes):
        return True
    if worktree:
        wt = worktree.replace("\\", "/").rstrip("/").lower()
        return low == wt or low.startswith(wt + "/")
    return False


def _check_segment(
    argv: list[str],
    redirects: list[tuple[str, str]],
    cfg: CommandGuardCfg,
    *,
    readonly: bool,
    secrets: GlobSet,
    worktree: str | None,
) -> CommandVerdict:
    for op, t in redirects:
        if t in SAFE_REDIRECT_TARGETS or t.lstrip("&").isdigit():
            continue
        if readonly and ">" in op:
            return _deny(
                "readonly_redirect",
                f"role chỉ đọc không được ghi ra file ({t[:40]})",
                "Chỉ chạy và đọc kết quả",
            )
        bad = _path_problem(t, cfg, secrets, worktree)
        if bad:
            return bad
    real = unwrap(argv)
    if not real:
        return OK
    name = prog_name(real[0])
    args = real[1:]
    if name in {b.lower() for b in cfg.deny_bins}:
        return _deny(
            "deny_bin",
            f"không được chạy {name}",
            "Dùng tool có kiểm soát (fs.*, git.*, test.run) hoặc escalation",
        )
    if readonly and name in {b.lower() for b in cfg.readonly_write_bins}:
        return _deny("readonly_write", f"role chỉ đọc không được chạy {name}", "Chỉ chạy và đọc kết quả")
    if readonly and name in ("sed", "perl") and any(a.startswith("-i") for a in args):
        return _deny("readonly_write", f"{name} -i sửa file tại chỗ", "Chỉ chạy và đọc kết quả")
    if name == "git":
        verdict = _check_git(args, cfg)
        if not verdict.ok:
            return verdict
    if name in ("rm", "del", "rd", "rmdir") and (
        RM_RECURSIVE & set(args) or {"/s", "/S", "/q", "/Q"} & set(args)
    ):
        return _deny("rm_recursive", f"xoá đệ quy bằng {name}", "Xoá từng file bằng fs.delete nếu thật cần")
    if name == "find" and ({"-delete", "-exec", "-execdir", "-ok"} & set(args)):
        return _deny("find_exec", "find -delete/-exec chạy lệnh ẩn", "Dùng find chỉ để liệt kê")
    verdict = _check_install(name, args, cfg)
    if not verdict.ok:
        return verdict
    verdict = _check_inline(name, args, cfg, readonly=readonly)
    if not verdict.ok:
        return verdict
    for a in args:
        bad = _path_problem(a, cfg, secrets, worktree)
        if bad:
            return bad
    return OK


def _check_git(args: list[str], cfg: CommandGuardCfg) -> CommandVerdict:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] not in ("--no-pager", "-P"):
            return _deny(
                "git_option", f"git {args[i]} đổi cấu hình hoặc thư mục git", "Gọi git subcommand trực tiếp"
            )
        i += 1
    if i >= len(args):
        return OK
    sub = args[i].lower()
    if sub not in {g.lower() for g in cfg.git_read_only}:
        return _deny(
            "git_write",
            f"git {sub} không chạy qua exec",
            "Dùng tool git.* (diff, status, commit trong worktree)",
        )
    rest = args[i + 1 :]
    if sub == "branch" and (GIT_BRANCH_WRITE & set(rest) or any(not a.startswith("-") for a in rest)):
        return _deny("git_write", "git branch chỉ được liệt kê", "Không tạo, đổi tên hay xoá nhánh")
    return OK


def _check_install(name: str, args: list[str], cfg: CommandGuardCfg) -> CommandVerdict:
    tool, rest = name, args
    if PYTHONS.match(name) and len(args) >= 2 and args[0] == "-m":
        tool, rest = prog_name(args[1]), args[2:]
    if tool == "pip3":
        tool = "pip"
    if tool in ("npx", "pnpx") and any(a in ("-y", "--yes") for a in rest):
        return _deny(
            "install", f"{tool} --yes tải và chạy gói lạ", "Dùng pkg.install (gói ghim phiên bản, có kiểm)"
        )
    verbs = cfg.install_verbs.get(tool)
    if not verbs:
        return OK
    verb = next((a for a in rest if not a.startswith("-")), "")
    if verb.lower() in verbs:
        return _deny(
            "install",
            f"{tool} {verb} đổi dependency",
            "Dùng pkg.install (không sửa manifest) hoặc pkg.add (sửa manifest, cần G2)",
        )
    return OK


def _check_inline(name: str, args: list[str], cfg: CommandGuardCfg, *, readonly: bool) -> CommandVerdict:
    code: str | None = None
    if PYTHONS.match(name):
        if "-c" in args and args.index("-c") + 1 < len(args):
            code = args[args.index("-c") + 1]
        if "-m" in args and args.index("-m") + 1 < len(args):
            mod = args[args.index("-m") + 1]
            if mod in cfg.module_deny:
                return _deny("module_deny", f"python -m {mod} bị chặn", "Không mở server, không cài gói")
    elif name in INLINE_FLAGS:
        for flag in INLINE_FLAGS[name]:
            if flag in args and args.index(flag) + 1 < len(args):
                code = args[args.index(flag) + 1]
                break
    if code is None:
        return OK
    for pat in cfg.inline_code_deny:
        if re.search(pat, code):
            return _deny(
                "inline_code",
                f"code inline dùng thứ bị chặn ({pat})",
                "Viết test hoặc script trong repo thay vì code inline",
            )
    if readonly and READONLY_CODE.search(code):
        return _deny("readonly_write", "code inline ghi file", "Role chỉ đọc: chỉ in ra, không ghi")
    return OK


def check_command(
    cmd: str,
    cfg: CommandGuardCfg,
    *,
    readonly: bool = False,
    scan: bool = False,
    worktree: str | None = None,
) -> CommandVerdict:
    """OK hoặc lý do chặn. readonly: Verifier · scan: Security (một lệnh đơn, không toán tử shell)."""
    if not cmd.strip():
        return OK
    if "$(" in cmd or "`" in cmd or "<(" in cmd or ">(" in cmd:
        return _deny("subshell", "lệnh con $( ) hoặc ` ` che lệnh thật", "Chạy từng lệnh riêng")
    tokens = tokenize(cmd)
    if tokens is None:
        return _deny("parse", "không tách được lệnh (ngoặc kép lệch)", "Kiểm lại dấu nháy")
    segments = split_segments(tokens)
    if scan and (len(segments) > 1 or any(t for _, t in segments)):
        return _deny(
            "scan_metachar", "profile scan chỉ chạy một lệnh đơn, không ; | > &", "Gọi đúng một scanner"
        )
    secrets = GlobSet(cfg.secret_globs)
    for argv, redirects in segments:
        verdict = _check_segment(argv, redirects, cfg, readonly=readonly, secrets=secrets, worktree=worktree)
        if not verdict.ok:
            return verdict
    # Tách kiểu POSIX nuốt dấu gạch ngược của đường dẫn Windows (cmd.exe khi không có bash): soát đường dẫn thêm
    # một lượt với gạch ngược đổi thành gạch chéo. Ngoặc lệch ở lượt này thì bỏ qua, lượt chính đã tách được.
    alt = tokenize(cmd.replace(BACKSLASH, "/"))
    for tok in alt or []:
        if tok in SEPARATORS or _is_redirect(tok):
            continue
        bad = _path_problem(tok, cfg, secrets, worktree)
        if bad:
            return bad
    return OK


__all__ = ["CommandVerdict", "check_command", "split_segments", "tokenize", "unwrap"]

"""Pydantic models cho 4 file config. Đây là NGUỒN SỰ THẬT cho config (xem README › Quyết định kiến trúc).

Expression (str) được compile ở checks/loader, không ở đây — model chỉ giữ chuỗi.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from digital_development_team.core.enums import PermissionMode, Tier


class _Cfg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _CfgLoose(BaseModel):
    """Cho phần free-form (match dict của tool rule)."""

    model_config = ConfigDict(frozen=True, extra="allow")


TierStr = Literal["T0", "T1", "T2", "T3"]


def _t0_only() -> list[TierStr]:  # default_factory cần list mới mỗi lần
    return ["T0"]


TierMap = dict[TierStr, int]
TierMapOpt = dict[TierStr, int | None]


# ───────────────────────────── policy.yaml ─────────────────────────────
class GateCfg(_Cfg):
    trigger: list[str] = Field(default_factory=list)  # expr, OR
    requires_human_when: list[str] = Field(default_factory=list)  # expr, OR
    timeout_sec: TierMapOpt = Field(default_factory=dict)
    default_on_timeout: (
        Literal["proceed_conservative", "proceed_with_recommendation", "block", "skip_issue"] | None
    ) = None
    t3_witnesses: int = 1
    batch_approve_tiers: list[TierStr] = Field(default_factory=list)
    session_approval_memory: bool = False
    stop_file: str | None = None


class GatesCfg(_Cfg):
    g0: GateCfg = GateCfg()
    g1: GateCfg = GateCfg()
    g2: GateCfg = GateCfg()
    g3: GateCfg = GateCfg()


class ConfidenceWeights(_Cfg):
    test_signal: float = 0.30
    verifier_agreement: float = 0.25
    spec_clarity: float = 0.20
    familiarity: float = 0.15
    rework_penalty: float = 0.20

    @property
    def max_possible(self) -> float:
        return self.test_signal + self.verifier_agreement + self.spec_clarity + self.familiarity


class AutonomyPreset(_Cfg):
    conf_proceed: float
    conf_bump_range: tuple[float, float]
    g1_t2_timeout: int | None
    g0_timeout: int
    dev_permission: PermissionMode
    auto_decide: bool = (
        False  # True: cổng KHÔNG chờ người — harness quyết ngay (thi đấu: người chạm máy = −50 điểm)
    )
    familiarity_unknown: float | None = None  # familiarity khi chưa có lịch sử module (None = 0.4 mặc định)


class TierRule(_Cfg):
    when: str | None = None  # None = default
    tier: str  # T0..T3 | "+1" | "$value"

    @model_validator(mode="after")
    def _tier_ok(self) -> TierRule:
        if self.tier not in ("T0", "T1", "T2", "T3", "+1", "+2", "$value"):
            raise ValueError(f"tier rule không hợp lệ: {self.tier!r}")
        return self


class TransitionRule(_Cfg):
    when: str | None = None  # None = default
    to: str | list[str]  # Stage hoặc [Stage, Stage] song song
    rework: Literal["+1"] | None = None
    reloc: Literal["+1"] | None = None
    mg_retries: Literal["+1"] | None = None
    bump: dict[str, Literal["+1"]] = Field(default_factory=dict)  # bộ đếm chung facts.counters → state.<tên>
    final: bool = False  # người đã chốt dừng → thang phục hồi không can thiệp
    cancel: str | None = None
    skip: bool = False
    rule_only: bool = False
    note: str | None = None


class ToolRuleMatch(_CfgLoose):
    tool: str | None = None
    role: str | None = None
    args_regex: str | None = None
    path_glob: str | None = None
    path_not_in: str | None = None
    path_in: str | None = None
    tier_below: TierStr | None = None
    cmd_not_in: list[str] | None = None
    count_per_issue_gte: int | None = None


class ToolRule(_Cfg):
    match: ToolRuleMatch | None = None  # None = default
    action: Literal["allow", "deny", "ask"]
    reason: str | None = None
    gate: Literal["G2"] | None = None
    constraint: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_default(self) -> bool:
        return self.match is None


class SandboxProfileCfg(_Cfg):
    writable: list[str] = Field(default_factory=list)
    cmd_allowlist: list[str] | None = None


class CommandGuardCfg(_Cfg):
    """Lớp 1 kiểm lệnh exec.run / test.run (policy/command.py). Mặc định ở đây để không bao giờ trống."""

    deny_bins: list[str] = Field(
        default_factory=lambda: [
            # mạng
            "curl",
            "wget",
            "nc",
            "ncat",
            "netcat",
            "ssh",
            "scp",
            "sftp",
            "ftp",
            "telnet",
            "rsync",
            "invoke-webrequest",
            "iwr",
            "invoke-restmethod",
            "irm",
            "bitsadmin",
            "certutil",
            # quyền, hệ thống
            "sudo",
            "su",
            "doas",
            "runas",
            "chown",
            "chmod",
            "icacls",
            "takeown",
            "reg",
            "regedit",
            "schtasks",
            "sc",
            "net",
            "netsh",
            "setx",
            "shutdown",
            "reboot",
            "kill",
            "pkill",
            "killall",
            "taskkill",
            # shell lồng, che lệnh
            "bash",
            "sh",
            "zsh",
            "dash",
            "fish",
            "cmd",
            "powershell",
            "pwsh",
            "wsl",
            "eval",
            "source",
            ".",
            # container, cloud, hạ tầng
            "docker",
            "podman",
            "kubectl",
            "helm",
            "terraform",
            "aws",
            "gcloud",
            "az",
            # cài gói hệ thống
            "apt",
            "apt-get",
            "yum",
            "dnf",
            "brew",
            "choco",
            "winget",
            "scoop",
            "pacman",
        ]
    )
    git_read_only: list[str] = Field(
        default_factory=lambda: [
            "status",
            "diff",
            "log",
            "show",
            "grep",
            "ls-files",
            "blame",
            "rev-parse",
            "describe",
            "shortlog",
            "branch",
        ]
    )
    install_verbs: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "pip": ["install", "uninstall", "download"],
            "npm": [
                "install",
                "i",
                "ci",
                "add",
                "remove",
                "rm",
                "uninstall",
                "publish",
                "update",
                "up",
                "link",
            ],
            "yarn": ["add", "install", "remove", "publish", "upgrade", "link"],
            "pnpm": ["add", "install", "i", "remove", "rm", "publish", "update", "up", "link"],
            "bun": ["add", "install", "i", "remove", "publish", "update", "link"],
            "poetry": ["add", "install", "remove", "update", "publish"],
            "uv": ["add", "remove", "sync", "pip", "tool"],
            "cargo": ["install", "publish"],
            "go": ["install", "get"],
            "dotnet": ["add", "remove", "restore", "tool"],
            "mvn": ["dependency:get", "install:install-file", "dependency:copy"],
            "gem": ["install", "uninstall"],
            "conda": ["install", "remove", "update"],
        }
    )
    inline_code_deny: list[str] = Field(
        default_factory=lambda: [
            r"\bsubprocess\b",
            r"\bos\.(system|popen|exec\w*|spawn\w*|remove|unlink|rmdir|removedirs)\b",
            r"\bpty\b",
            r"\bsocket\b",
            r"\burllib\b",
            r"\bhttp\.client\b",
            r"\brequests\b",
            r"\bhttpx\b",
            r"\bftplib\b",
            r"\bsmtplib\b",
            r"\bshutil\.rmtree\b",
            r"__import__",
            r"\bimportlib\b",
            r"child_process",
            r"require\(\s*['\"](https?|net|dgram|tls)['\"]\s*\)",
            r"\bfetch\s*\(",
        ]
    )
    module_deny: list[str] = Field(default_factory=lambda: ["pip", "http.server", "smtpd", "ftplib", "venv"])
    secret_globs: list[str] = Field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            ".envrc",
            "*.pem",
            "*.key",
            "*.pfx",
            "*.p12",
            "*.jks",
            "id_rsa*",
            "id_ed25519*",
            "id_ecdsa*",
            "id_dsa*",
            ".git-credentials",
            ".netrc",
            ".npmrc",
            ".pypirc",
            "credentials*.json",
            "service-account*.json",
            "*.tfstate",
            "secrets.y*ml",
            ".ssh",
            ".aws",
            ".gnupg",
            ".kube",
        ]
    )
    readonly_write_bins: list[str] = Field(
        default_factory=lambda: [
            "rm",
            "rmdir",
            "del",
            "erase",
            "rd",
            "mv",
            "move",
            "ren",
            "rename",
            "cp",
            "copy",
            "xcopy",
            "robocopy",
            "touch",
            "tee",
            "truncate",
            "mkdir",
            "ln",
            "dd",
            "patch",
            "install",
        ]
    )
    # chuỗi để so khớp đường dẫn trong lệnh, không tạo file tạm
    allow_abs_prefixes: list[str] = Field(default_factory=lambda: ["/dev/null", "/tmp/", "nul"])  # noqa: S108


class SandboxCfg(_Cfg):
    kind: Literal["local", "docker"] = "local"
    image: str = "ddt-runner:latest"
    network: Literal["none", "allowlist", "full"] = "none"
    network_allowlist: list[str] = Field(default_factory=list)
    cpu: int = 2
    memory: str = "4g"
    timeout_sec: int = 120
    readonly_root: bool = True
    profiles: dict[str, SandboxProfileCfg] = Field(default_factory=dict)


class CondenserCfg(_Cfg):
    artifactize_threshold: int = 2000
    evict_at: float = 0.80
    compress_at: float = 0.90
    keep_last_turns: int = 6
    memory_block_tokens: int = 1200
    fresh_window_max_per_issue: int = 1


class DelegationRoleCfg(_Cfg):
    max_subagents: int


class DelegationCfg(_Cfg):
    enabled: bool = True
    max_depth: int = 1
    sub_budget_pct: float = 0.40
    min_budget_remaining_pct: float = 0.60
    max_retry_per_sub: int = 1
    collect_timeout_pct: float = 0.70
    sub_tools_never: list[str] = Field(default_factory=list)
    review_checklists: list[str] = Field(default_factory=list)
    roles: dict[str, DelegationRoleCfg] = Field(default_factory=dict)


class ClarifyCfg(_Cfg):
    enabled: bool = True
    max_per_issue_per_agent: int = 1
    timeout_sec: int = 60
    pairs: dict[str, list[str]] = Field(default_factory=dict)
    answerer_model: str = "sonnet"


class ChecklistsCfg(_Cfg):
    enabled: bool = True
    max_items_per_brief: int = 30
    machine_first: bool = True
    learned_max_active_per_repo: int = 30
    learned_initial_conf: float = 0.5
    learned_never_blocking: bool = True


class RitualsCfg(_Cfg):
    retro_every: int = 10
    retro_model: str = "opus"
    peer_review_tiers: list[TierStr] = Field(default_factory=list)
    onboard_block: bool = True
    handoff_field: bool = True


class CanaryCfg(_Cfg):
    enabled: bool = True
    rate: float = 0.20
    tiers: list[TierStr] = ["T1", "T2"]
    mutations: list[str] = Field(default_factory=list)
    catch_rate_threshold: float = 0.70


class MemoryCfg(_Cfg):
    """Tự học (50-knowledge): fact/skill Learner tạo đi quarantine → candidate → active; harness chấm theo kết quả
    issue đã dùng nó (memory/learned.py · memory/facts.py), người duyệt/gỡ bất cứ lúc nào."""

    promote_min_evidence: int = 2  # fact Learner đề xuất cần ≥ N issue pass làm bằng chứng
    promote_initial_conf: float = 0.5
    conf_on_pass: float = 0.15
    conf_on_fail: float = -0.30  # issue dùng nó bị BLOCKED hoặc người bác
    conf_on_soft_fail: float = -0.10  # issue xong nhưng role phải rework
    conf_delete_below: float = 0.30  # dưới ngưỡng → retired
    auto_promote_applied: int = 3  # candidate → active sau N lần dùng đo được …
    auto_promote_rate: float = 0.75  # … với tỉ lệ pass ≥ ngưỡng này
    skill_max_tokens: int = 400
    skills_per_brief: int = 3  # active
    candidates_per_brief: int = 1  # candidate, có nhãn chưa kiểm chứng
    episodes_per_brief: int = 3
    learn_min_tool_calls: int = 25  # issue pass mà dài cỡ này → đáng học
    learn_max_attempts: int = 2


SearchBackendName = Literal["claude_cli", "anthropic_api", "brave"]
_SEARCH_BACKENDS: tuple[SearchBackendName, ...] = ("claude_cli", "anthropic_api", "brave")


class WebHostCfg(_Cfg):
    host: str  # khớp chính nó và mọi subdomain (docs.python.org khớp cả docs.python.org/3/…)
    trust: Literal["docs", "ugc"] = (
        "docs"  # ugc: nội dung người dùng (forum, repo) — không phải nguồn chính thức
    )


_DOC_HOSTS = (
    "docs.python.org",
    "peps.python.org",
    "pypi.org",
    "readthedocs.io",
    "readthedocs.org",
    "docs.pytest.org",
    "docs.pydantic.dev",
    "docs.djangoproject.com",
    "flask.palletsprojects.com",
    "fastapi.tiangolo.com",
    "numpy.org",
    "pandas.pydata.org",
    "developer.mozilla.org",
    "nodejs.org",
    "npmjs.com",
    "typescriptlang.org",
    "react.dev",
    "angular.dev",
    "vuejs.org",
    "nextjs.org",
    "vitest.dev",
    "jestjs.io",
    "docs.oracle.com",
    "docs.spring.io",
    "maven.apache.org",
    "gradle.org",
    "junit.org",
    "kotlinlang.org",
    "learn.microsoft.com",
    "nuget.org",
    "npmjs.org",
    "go.dev",
    "pkg.go.dev",
    "docs.rs",
    "doc.rust-lang.org",
    "docs.github.com",
    "git-scm.com",
    "json-schema.org",
    "yaml.org",
    "rfc-editor.org",
    "datatracker.ietf.org",
    "osv.dev",
    "nvd.nist.gov",
    "cwe.mitre.org",
    "owasp.org",
    "semver.org",
    "sqlite.org",
    "postgresql.org",
    "dev.mysql.com",
)
_UGC_HOSTS = (
    "github.com",
    "raw.githubusercontent.com",
    "stackoverflow.com",
    "stackexchange.com",
    "wikipedia.org",
)


McpRisk = Literal["read", "soft_write", "hard_write"]
_LAUNCHERS = {"npx", "uvx", "pipx", "bunx", "pnpm", "yarn", "dlx"}
_PINNED = re.compile(r"^(@?[\w.\-/]+)(@|==)v?\d[\w.\-+]*$")


class McpServerCfg(_Cfg):
    """Một MCP server ngoài (Phase 10). Tool lộ ra = `mcp.<name>__<tool>`, chỉ những tool khai trong `tools`."""

    name: str = Field(pattern=r"^[a-z][a-z0-9]{1,19}$")
    transport: Literal["stdio", "http"] = "stdio"
    command: list[str] = Field(default_factory=list)  # stdio: argv; gói qua npx/uvx… phải ghim phiên bản
    url: str | None = None  # http: phải nằm trong policy.network.allow
    env: dict[str, str] = Field(
        default_factory=dict
    )  # chỉ các biến này tới server; "${NAME}" = lấy NAME từ máy
    roles: list[str] = Field(min_length=1)
    tools: dict[str, McpRisk] = Field(
        min_length=1
    )  # tool → risk tier; tool không khai = ẩn; không có destructive
    max_calls_per_issue: int = Field(default=20, ge=1)
    timeout_sec: int = Field(default=30, ge=1, le=600)
    pinned_sha: str | None = None  # sha256 schema tools/list lúc `ddt ext mcp pin`; lệch → từ chối

    @model_validator(mode="after")
    def _transport(self) -> McpServerCfg:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(f"mcp {self.name}: stdio cần command")
            prog = (
                self.command[0]
                .replace("\\", "/")
                .rsplit("/", 1)[-1]
                .lower()
                .removesuffix(".cmd")
                .removesuffix(".exe")
            )
            if prog in _LAUNCHERS and not any(_PINNED.match(a) for a in self.command[1:]):
                raise ValueError(
                    f"mcp {self.name}: {prog} phải ghim phiên bản gói (vd pkg@1.2.3 hoặc pkg==1.2.3)"
                )
        elif not self.url:
            raise ValueError(f"mcp {self.name}: http cần url")
        return self


class IntakeConsistencyCfg(_Cfg):
    """Phase 11, mặc định tắt: Intake tự tin thấp → phân loại lần hai, lệch thì giữ bản thận trọng."""

    enabled: bool = False
    below_confidence: float = Field(default=0.6, ge=0, le=1)


class McpClientCfg(_Cfg):
    enabled: bool = True
    servers: list[McpServerCfg] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> McpClientCfg:
        names = [s.name for s in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("mcp_client.servers: tên server trùng")
        return self


class GameMasterCfg(_Cfg):
    """Game Master của cuộc thi (harness gọi, agent không). Đường dẫn là template — đổi theo spec thật ở đây.

    Token đọc từ biến môi trường token_env; process agent (Codex/Claude) không thấy biến DDT_*.
    """

    base_url: str | None = None  # vd https://gm.example/api — None = chưa cấu hình
    adapter_factory: str | None = None  # explicit local module:function bridging the organizer's Game Player
    http_contract_confirmed: bool = False
    bearer_actions: list[Literal["match", "start_working", "submit", "submission"]] = Field(default_factory=list)
    final_grade_timeout_s: float = Field(default=300.0, ge=0)
    snapshot_max_age_s: float = Field(default=30.0, gt=0)
    retry_backoff_s: float = Field(default=15.0, gt=0)
    allow_unpushed_fake: bool = False
    token_env: str = "DDT_GM_TOKEN"  # noqa: S105 — tên biến môi trường, không phải mật khẩu
    team_header: str | None = None  # vd X-Team-Id nếu spec yêu cầu
    team_env: str = "DDT_GM_TEAM"
    paths: dict[str, str] = Field(
        default_factory=lambda: {
            "match": "/matches/{match_id}",
            "start_working": "/matches/{match_id}/backlog/{item_id}/start-working",
            "submit": "/matches/{match_id}/backlog/{item_id}/submissions",
            "submission": "/matches/{match_id}/backlog/{item_id}/submissions/{submission_id}",
        }
    )
    poll_s: float = Field(
        default=4.0, ge=1.0
    )  # tần suất đọc trang match — không dội tải (luật: cấm gây gián đoạn GM)
    timeout_s: float = Field(default=15.0, gt=0)
    retries: int = Field(default=3, ge=0, le=8)
    grade_poll_s: float = Field(default=10.0, ge=2.0)
    match_folder: str = "{match_id}"  # thư mục trong repo đội nơi GM đẩy source (main/<MatchID>/)
    push_remote: str | None = (
        "origin"  # missing remote blocks real submission; unpushed fake needs explicit opt-in
    )
    pi_slot: bool = True  # giữ 1 slot worker cho PI (điểm PI theo thứ hạng tốc độ)

    @model_validator(mode="after")
    def _paths(self) -> GameMasterCfg:
        missing = {"match", "start_working", "submit", "submission"} - set(self.paths)
        if missing:
            raise ValueError(f"gamemaster.paths thiếu {sorted(missing)}")
        return self


class NetworkCfg(_Cfg):
    """Mạng cho agent (user chốt 2026-10-11): tra cứu web + tài liệu CHỈ trong allowlist; cài gói chỉ từ registry
    chính thức. Mọi lệnh mạng khác vẫn bị command guard chặn."""

    web_enabled: bool = True
    search_backends: list[SearchBackendName] = Field(default_factory=lambda: list(_SEARCH_BACKENDS))
    allow: list[WebHostCfg] = Field(
        default_factory=lambda: (
            [WebHostCfg(host=h) for h in _DOC_HOSTS] + [WebHostCfg(host=h, trust="ugc") for h in _UGC_HOSTS]
        )
    )
    max_bytes: int = Field(default=2_000_000, ge=10_000)
    timeout_s: float = Field(default=20.0, gt=0)
    cache_ttl_h: int = Field(default=72, ge=0)
    max_url_chars: int = 512
    max_query_chars: int = 200
    max_redirects: int = 5
    block_private_ips: bool = True
    pkg_min_age_days: int = Field(default=14, ge=0)  # bản phát hành mới hơn → pkg.* từ chối
    pkg_checks: Literal["best_effort", "required"] = (
        "best_effort"  # OSV/tuổi không kiểm được: cài + cảnh báo | từ chối
    )
    registries: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "pypi": ["pypi.org", "files.pythonhosted.org"],
            "npm": ["registry.npmjs.org"],
            "maven": ["repo.maven.apache.org", "repo1.maven.org"],
            "nuget": ["api.nuget.org"],
            "go": ["proxy.golang.org", "sum.golang.org"],
        }
    )


class TaskTypeCfg(_Cfg):
    """Profile theo intake.type (policy.yaml#task_types): luật riêng mỗi loại task, đưa vào CONSTRAINTS của brief."""

    test_command: Literal["required", "optional"] = "required"  # preflight của IMPLEMENT kiểm
    budget_multiplier: float = Field(default=1.0, gt=0, le=4)
    dev: list[str] = Field(default_factory=list)
    verifier: list[str] = Field(default_factory=list)
    analyst: list[str] = Field(default_factory=list)


class SpecAmendCfg(_Cfg):
    """Sửa spec giữa chừng (runner/spec_amend.py). Tự duyệt chỉ khi thêm ít file test / cùng thư mục, không đổi lệnh."""

    max_allowed_files: int = Field(default=20, ge=1, le=50)
    auto_enabled: bool = True
    auto_max_paths: int = Field(default=3, ge=0)
    auto_max_per_issue: int = Field(default=1, ge=0)


FAILURE_CLASSES = (
    # còn tự cứu được
    "test_fail",
    "no_output",
    "stuck",
    "agent_gave_up",
    "scope",
    "test_command",
    "spec_conflict",
    "base_moved",
    "budget_stage",
    "budget_issue",
    "too_large",
    "verifier_unavailable",
    # cần người
    "policy_denied",
    "destructive",
    "needs_info",
    "needs_decision",
    "environment",
    "human_abort",
    "cancelled",
)


class RecoveryRungCfg(_Cfg):
    rung: str
    max: int = Field(default=1, ge=0)
    classes: list[str]  # lớp lỗi khớp bậc này (khoá `on` bị YAML 1.1 đọc thành True)
    to: str = "auto"  # auto = stage vừa hỏng nếu là stage agent, không thì IMPLEMENT · hoặc tên stage
    model_role: str | None = None  # vd "{role}_strong" → models.yaml#roles; thiếu cấu hình → bỏ qua bậc
    mode: str | None = None
    stages: list[str] | None = None  # chỉ áp khi hỏng ở các stage này (None = mọi stage)

    @field_validator("classes")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in FAILURE_CLASSES]
        if bad:
            raise ValueError(f"lớp lỗi lạ {bad}; có: {list(FAILURE_CLASSES)}")
        return v


class RecoveryCfg(_Cfg):
    """Thang phục hồi (runner/recovery.py): sắp BLOCKED → phân loại lỗi (0 LLM) → bậc đầu còn lượt khớp lớp lỗi."""

    enabled: bool = True
    extra_budget_pct: int = Field(default=25, ge=0, le=200)  # trần issue cộng thêm khi đang phục hồi
    max_reopens: int = Field(default=2, ge=0)
    ladder: list[RecoveryRungCfg] = Field(default_factory=list)


class BlastRadiusCfg(_Cfg):
    """Blast radius của patch (MACHINE_GATE, 0 LLM): depth BFS CodeGraph + ngưỡng FMEA RPN (risk/blast.py)."""

    depth: int = Field(default=2, ge=1, le=5)
    rpn_high: int = Field(default=200, ge=1)
    rpn_medium: int = Field(default=80, ge=1)


class PolicyConfig(_Cfg):
    version: int = 1
    # vận hành
    wip_limit: int = 3
    wip_max: int = 5
    autonomy: Literal["conservative", "balanced", "aggressive", "autonomous"] = "balanced"
    sweep_mode: bool = False
    economy_mode: bool = False
    minimal: bool = False
    no_learn: bool = False
    transcripts: bool = True  # ghi work/<id>/transcripts/*.jsonl (Process view); --no-transcripts tắt
    retriage_interval_sec: int = 600
    leader_persistent_context: bool = True
    leader_ctx_compress_at: int = 40000
    leader_ctx_reset_at: int = 60000
    # gates / confidence
    gates: GatesCfg = GatesCfg()
    confidence_weights: ConfidenceWeights = ConfidenceWeights()
    conf_proceed: float = 0.80
    conf_bump_range: tuple[float, float] = (0.50, 0.80)
    conf_escalate: float = 0.50
    autonomy_presets: dict[str, AutonomyPreset] = Field(default_factory=dict)
    gate_rule_tiers: list[TierStr] = Field(default_factory=_t0_only)  # GATE theo rule, không gọi Leader
    review_skip_tiers: list[TierStr] = Field(default_factory=_t0_only)  # bỏ REVIEW khi regex bảo mật sạch
    split_enabled: bool = True  # plan.subtasks → SPLIT tạo issue con (thi đấu: mỗi item nộp đúng 1 lần → tắt)
    deadline_margin_s: int = Field(
        default=90, ge=0
    )  # dừng làm trước hạn item bấy nhiêu giây (đủ push + submit)
    tool_denylist: list[str] = Field(
        default_factory=list
    )  # fnmatch tên tool gỡ khỏi mọi role (--match: tool không ra điểm)
    # tier
    tier_rules: list[TierRule] = Field(default_factory=list)
    # retry
    max_rework: TierMap = {"T0": 1, "T1": 2, "T2": 2, "T3": 3}
    max_reloc: TierMap = {"T0": 0, "T1": 1, "T2": 1, "T3": 1}
    machine_gate_free_retries: int = 2
    slot_write_attempts: int = 3
    rework_model_escalation_at: int = 2
    spec_amend: SpecAmendCfg = SpecAmendCfg()
    recovery: RecoveryCfg = RecoveryCfg()
    network: NetworkCfg = NetworkCfg()
    gamemaster: GameMasterCfg = GameMasterCfg()
    mcp_client: McpClientCfg = McpClientCfg()
    intake_consistency: IntakeConsistencyCfg = IntakeConsistencyCfg()
    task_types: dict[str, TaskTypeCfg] = Field(default_factory=dict)

    def task_profile(self, task_type: str | None) -> TaskTypeCfg:
        """Profile của loại task; thiếu → `default` → profile rỗng (luật trong JD áp nguyên)."""
        return self.task_types.get(task_type or "") or self.task_types.get("default") or TaskTypeCfg()

    # transitions / tool rules
    transitions: dict[str, list[TransitionRule]] = Field(default_factory=dict)
    tool_rules: list[ToolRule] = Field(default_factory=list)
    # isolation
    exclusive_files: list[str] = Field(default_factory=list)
    lock_lease_sec: int = 1800
    merge_serialize: bool = True
    machine_gate_after_rebase: bool = True
    # subsystems
    sandbox: SandboxCfg = SandboxCfg()
    command_guard: CommandGuardCfg = CommandGuardCfg()
    condenser_defaults: CondenserCfg = CondenserCfg()
    delegation: DelegationCfg = DelegationCfg()
    clarify: ClarifyCfg = ClarifyCfg()
    checklists: ChecklistsCfg = ChecklistsCfg()
    rituals: RitualsCfg = RitualsCfg()
    canary: CanaryCfg = CanaryCfg()
    codegraph_impact_thresholds: dict[str, int] = Field(default_factory=lambda: {"low": 3, "medium": 8})
    blast_radius: BlastRadiusCfg = BlastRadiusCfg()
    memory: MemoryCfg = MemoryCfg()

    def tier_limit(self, table: TierMap, tier: Tier) -> int:
        return table[tier.value]

    def effective_autonomy(self) -> AutonomyPreset | None:
        return self.autonomy_presets.get(self.autonomy)

    def effective_conf_proceed(self) -> float:
        preset = self.effective_autonomy()
        return preset.conf_proceed if preset is not None else self.conf_proceed

    def effective_conf_bump_range(self) -> tuple[float, float]:
        preset = self.effective_autonomy()
        return preset.conf_bump_range if preset is not None else self.conf_bump_range

    @property
    def auto_decide(self) -> bool:
        """Cổng do harness quyết ngay, không chờ người (autonomy `autonomous`)."""
        preset = self.effective_autonomy()
        return bool(preset and preset.auto_decide)


# ───────────────────────────── budgets.yaml ─────────────────────────────
class StageBudget(_Cfg):
    turn: int = Field(ge=1)
    wall: float = Field(gt=0)  # phút
    tokens: int = Field(ge=1)


class StageBudgets(_Cfg):
    T0: StageBudget | None = None
    T1: StageBudget | None = None
    T2: StageBudget | None = None
    T3: StageBudget | None = None
    any: StageBudget | None = None
    change_request_multiplier: float | None = None
    rework_bump: int | None = None

    def for_tier(self, tier: Tier) -> StageBudget:
        b = getattr(self, tier.value) or self.any
        if b is None:
            raise ValueError(f"không có budget cho tier {tier}")
        return b


class IssueBudget(_Cfg):
    tokens: int = Field(ge=1)
    wall: float = Field(gt=0)  # phút


class SessionBudget(_Cfg):
    tokens: int = 5_000_000
    usd: float = 100.0
    warn_at: float = 0.60
    economy_at: float = 0.80
    stop_new_at: float = 0.95
    reserve_for_t3: float = 0.05


class LlmAskBudget(_Cfg):
    per_call_tokens: int = 2000
    per_stage_calls: int = 5
    model: str = "haiku"


class ToolTimeouts(_Cfg):
    fs: int = 5
    exec: int = 120
    test: int = 240
    codegraph: int = 10
    blackboard: int = 5
    memory: int = 5
    user_ask: Literal["inherit"] | int = "inherit"
    llm_ask: LlmAskBudget = LlmAskBudget()


class TransportOverhead(_Cfg):
    """Phụ phí mỗi lượt theo transport (CLI: system prompt riêng của CLI + spawn process). 0 = không cộng.
    turn_factor: nhân trần lượt — Codex báo từng tool call (không phải từng lượt model) nên 1 lượt model ≈ vài call."""

    tokens_per_turn: int = Field(default=0, ge=0)
    wall_s_per_turn: float = Field(default=0.0, ge=0)
    turn_factor: float = Field(default=1.0, ge=1.0)


class BudgetsConfig(_Cfg):
    version: int = 1
    stages: dict[str, StageBudgets]
    issue: dict[TierStr, IssueBudget]
    session: SessionBudget = SessionBudget()
    warnings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tools: ToolTimeouts = ToolTimeouts()
    economy: dict[str, Any] = Field(default_factory=dict)
    transport_overhead: dict[str, TransportOverhead] = Field(default_factory=dict)  # key: cli | codex


# ───────────────────────────── models.yaml ─────────────────────────────
class CliCfg(_Cfg):
    """Cờ bắt buộc cho transport CLI (Claude Code headless). Thiếu/sai → config check fail."""

    tools: list[str] = Field(default_factory=list)  # [] = tắt toàn bộ built-in tool, chỉ MCP DDT
    mcp_config: str = "state/ddt-mcp.json"
    setting_sources: list[str] = Field(
        default_factory=lambda: ["user"]
    )  # KHÔNG nạp CLAUDE.md/.claude repo đích
    permission_prompt_tool: str | None = "mcp__ddt__policy"
    output_format: Literal["stream-json", "json"] = "stream-json"
    max_turns_from: str | None = None  # CLI 2.1 không có --max-turns; harness đếm turn và kill

    @model_validator(mode="after")
    def _safe(self) -> CliCfg:
        if self.tools:
            raise ValueError("cli.tools phải rỗng — built-in tool của CLI đi vòng qua policy DDT")
        if "project" in self.setting_sources or "local" in self.setting_sources:
            raise ValueError(
                "cli.setting_sources không được chứa project/local — kênh prompt injection từ repo đích"
            )
        return self


CODEX_REQUIRED_OFF = ("shell_tool",)  # built-in đi vòng qua policy DDT — bắt buộc tắt


class CodexCfg(_Cfg):
    """Cờ cho transport Codex CLI (`codex exec`). Mọi tool đi qua MCP DDT; built-in của Codex tắt hoặc bị sandbox chặn."""

    disable_features: list[str] = Field(
        default_factory=lambda: [
            "shell_tool",
            "apps",
            "plugins",
            "browser_use",
            "computer_use",
            "multi_agent",
            "image_generation",
            "hooks",
            "goals",
            "in_app_browser",
            "skill_search",
            "tool_suggest",
        ]
    )
    sandbox: Literal["read-only"] = (
        "read-only"  # apply_patch built-in bị chặn → chỉ fs.write/edit của DDT ghi file
    )
    ignore_user_config: bool = (
        True  # không nạp ~/.codex/config.toml (MCP/plugin cá nhân); auth vẫn từ CODEX_HOME
    )
    ignore_rules: bool = True
    project_doc_max_bytes: int = 0  # KHÔNG nạp AGENTS.md của repo đích — kênh prompt injection
    codex_home: str | None = None  # CODEX_HOME riêng (tài khoản BTC); None = mặc định của máy
    mcp_startup_timeout_s: int = 60  # `ddt mcp` dựng runtime khá chậm
    mcp_tool_timeout_s: int = 600  # test.run có thể chạy vài phút (mặc định Codex 60 s là quá ngắn)
    extra_config: dict[str, Any] = Field(default_factory=dict)  # -c key=value bổ sung (giá trị TOML)

    @model_validator(mode="after")
    def _safe(self) -> CodexCfg:
        missing = [f for f in CODEX_REQUIRED_OFF if f not in self.disable_features]
        if missing:
            raise ValueError(f"codex.disable_features phải chứa {missing} — built-in đi vòng qua policy DDT")
        if self.project_doc_max_bytes != 0:
            raise ValueError(
                "codex.project_doc_max_bytes phải 0 — AGENTS.md repo đích là kênh prompt injection"
            )
        return self


class ProviderCfg(_Cfg):
    adapter: Literal[
        "anthropic", "openai", "openai_compatible", "gemini", "bedrock", "mock", "claude_cli", "codex_cli"
    ]
    api_key_env: str | None = None
    base_url: str | None = None
    region_env: str | None = None
    supports: list[Literal["tool_use", "prompt_cache", "streaming", "agent_loop"]] = Field(
        default_factory=list
    )
    enabled: bool = False
    # transport CLI
    binary: str | None = None  # vd "claude" — trong PATH; pin version
    auth: Literal["api_key", "subscription"] | None = None
    cli: CliCfg | None = None
    codex: CodexCfg | None = None

    @model_validator(mode="after")
    def _cli_block(self) -> ProviderCfg:
        if self.adapter == "claude_cli" and self.cli is None:
            raise ValueError(
                "adapter claude_cli cần block cli: (tools · mcp_config · setting_sources · permission_prompt_tool …)"
            )
        if self.adapter == "codex_cli" and self.codex is None:
            raise ValueError("adapter codex_cli cần block codex: (disable_features · sandbox …)")
        return self

    @property
    def is_cli(self) -> bool:
        """Transport CLI (agent loop chạy trong process ngoài, tool qua MCP DDT)."""
        return self.adapter in ("claude_cli", "codex_cli")


class AliasCfg(_Cfg):
    provider: str
    model: str
    family: str
    effort: Literal["low", "medium", "high"] | None = None


class RoleModelCfg(_Cfg):
    model: str  # alias
    fallback: str | None = None
    prefer: str | None = None
    must_differ_family_from: str | None = None


class RateCfg(_Cfg):
    rpm_cap: int = 50
    tpm_cap: int = 400_000
    backoff: list[float] = [1.0, 2.0, 4.0, 8.0, 16.0]
    on_429_consecutive: int = 3
    fallback_cooldown_sec: int = 300
    per_provider: bool = True


class PriceCfg(_Cfg):
    in_: float = Field(alias="in")
    out: float
    cached_in: float | None = None
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ModelsConfig(_Cfg):
    version: int = 2
    providers: dict[str, ProviderCfg]
    aliases: dict[str, AliasCfg | None]
    roles: dict[str, RoleModelCfg]
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rate: RateCfg = RateCfg()
    pricing: dict[str, PriceCfg] = Field(default_factory=dict)
    pricing_by_provider: dict[str, PriceCfg] = Field(
        default_factory=dict
    )  # ghi đè theo provider (CLI subscription = 0)


# ───────────────────────────── sensitive_paths.yaml ─────────────────────────────
class SensitivePath(_Cfg):
    glob: str
    min_tier: TierStr
    reason: str | None = None


class SensitivePathsConfig(_Cfg):
    version: int = 1
    paths: list[SensitivePath]
    never_touch: list[str] = Field(default_factory=list)

    def globs(self) -> list[str]:
        return [p.glob for p in self.paths]

    def min_tier_for(self, rel_posix: str) -> Tier | None:
        from digital_development_team.util.globs import GlobSet

        best: Tier | None = None
        for p in self.paths:
            if GlobSet([p.glob]).match(rel_posix):
                t = Tier(p.min_tier)
                best = t if best is None or t.rank > best.rank else best
        return best

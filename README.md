# Digital-Worker — Digital Development Team per_role

Harness điều phối nhiều AI agent xử lý backlog (bug · change request · production incident · task) trên repo có sẵn.
Chế độ thường: con người giữ quyền **phát hành** (G1) và quyền **dừng** (G3). Chế độ thi đấu (`--match`, cuộc thi
Master the Machine 2026): từ Start Match không ai chạm máy — harness tự quyết mọi cổng, tự nộp bài lên Game Master.

Thiết kế: `../Plan/DDT-Design-Document.md` · tham chiếu: `../Plan/digital_workers/` · quyết định kiến trúc: mục cuối README này.

**Bản contest 17/10/2026:** [hướng dẫn chạy Codex và hai profile](docs/CONTEST-CODEX-RUNBOOK.vi.md) ·
[kết quả triển khai/kiểm chứng](docs/CONTEST-IMPLEMENTATION-2026-10-17.md).
Luồng mô phỏng đã được kiểm chứng; bridge Game Player, auth, READY, template và tài khoản model của BTC cần xác nhận trước thi thật.

## Trạng thái

| Phase | Nội dung | Trạng thái |
|---|---|---|
| 1 | Walking skeleton: 1 issue → commit trên main, 7 stage thật, G1 chờ người, replay/report | **Xong** — 418 test, E2E mock trên `testdata/repos/mini_calc` |
| 1c | **Skill import + tool chất lượng**: lớp `skills/imported/` (TDD, systematic-debugging, differential-review, py-review, ask-questions — MIT/CC-BY-SA, pin commit) · `security.scan` (bandit · detect-secrets · pip-audit) · `test.coverage` (pytest-cov + diff-cover) · `test.run(affected)` (testmon) · `codegraph.*` qua CodeGraph CLI · blast radius + FMEA trong MACHINE_GATE | **Xong** — mọi scanner/coverage là dependency chính; `codegraph.*` tự fallback PyGraph (stdlib ast) khi không có CodeGraph CLI; `ddt config check` cảnh báo tool thiếu |
| 1b | **Transport Claude Code CLI** song song API (`--provider cli`, subscription, 0 USD): `claude -p` + MCP server DDT (`ddt mcp`), cùng policy/allowlist/events | **Xong** — chạy thật 2026-10-03 trên `testdata/repos/shop_api` + `backlogs/shop.csv`: 7 stage → G1 → MERGE → commit trên main |
| 2s | **Thi đấu MTM 2026**: Codex CLI · MVS/evidence gate · ledger nộp bài · pause/resume · late grading · Game Player adapter factory | **Đã kiểm chứng mô phỏng** 2026-10-17 — còn contract test và rehearsal với BTC; xem runbook phía trên |
| 1d | **`ddt worker`** chạy pipeline liên tục (heartbeat, STOP < 5s, hot-reload override) | **Xong** — `test_worker.py`. Dashboard web (`ddt serve`, `view/`) đã gỡ 2026-10-16: ngoài phạm vi cuộc thi |
| 1e | **Minh bạch quá trình**: transcript store `work/<id>/transcripts/<STAGE>.<attempt>.jsonl` (brief 4 khối · từng turn · tool args/kết quả · harness message · outcome, đã redact) · biên bản bàn giao `ddt handover ISSUE` · `ddt report --write` · `<home>/reports/` | **Xong Increment 2** — `tests/integration/test_transcripts.py`, `tests/unit/reporting/` |
| 1g | **Đánh giá và học hỏi tất định (0 LLM)**: issue kết thúc (done/blocked) → runner ghi `role_outcomes` (pass theo định nghĩa từng role, kèm phiên bản JD), `episodes`, `worker_stats`, dòng `state/standup.md`; mỗi 10 issue ghi `reports/perf_NNN.md` · familiarity trong confidence lấy từ tỷ lệ pass thật của Dev trên module (≥ 2 lần, tắt bằng `--no-learn`) · `ddt perf [--write] [--backfill]` · worker dựng lại runtime giữa hai issue khi JD/skill/checklist/tool/config đổi, không cần khởi động lại | **Xong Increment 4** — `tests/unit/memory/test_outcomes.py` |
| 1h | **Escalation và câu hỏi đúng thiết kế**: agent nộp `escalation.yaml` → gate người (trước bước code G0, từ IMPLEMENT G1) → câu trả lời quay về đúng stage đã hỏi, vào brief mục Human answers, lưu `resolved` trong form, `human_override` khi người chọn khác khuyến nghị · option có thể ghi `route` (continue · rework · re_localize · merge · abort) · mỗi role hỏi tối đa 2 lần, lần 3 harness chọn khuyến nghị · form kém bị từ chối trước khi tới người · issue loại câu hỏi đi TRIAGE → ANSWER → DONE: Dev ở hồ sơ chỉ đọc (`stage_modes.ANSWER`), chỉ thấy câu hỏi Intake viết lại, ghi `answer.yaml`, harness quét secret trước khi đóng | **Xong** — `tests/unit/runner/test_escalation.py`, `tests/unit/runner/test_answer.py`, `tests/integration/test_answer_route.py` |
| 1i | **Siết ranh giới khi chưa có Docker**: kiểm lệnh `exec.run` · `test.run` · lệnh gate có cấu trúc (`policy/command.py`, danh sách trong `policy.yaml#command_guard`): tách theo toán tử shell, bỏ lớp bọc, chặn chương trình mạng/quyền/shell lồng, git chỉ đọc, cài gói, code inline nguy hiểm, file bí mật, đường dẫn ra ngoài worktree; thay deny-list regex từng bị lách · `test.run` của Dev chỉ chạy test runner · nút autonomy conservative có tác dụng với exec · Verifier lỡ ghi file thì tự hoàn tác từ HEAD · redact thêm mật khẩu trong URL, Basic auth, Stripe, `*_PASSWORD=`, redact đệ quy kết quả tool, che cả lệnh ghi vào event log · `fs.search` bỏ file bí mật · nội dung backlog không đóng được thẻ untrusted · stage hết giờ → circuit breaker + BLOCKED thay vì pause lặp vô hạn | **Xong** — `tests/unit/policy/test_command_guard.py`, `tests/unit/runner/test_stage_deadline.py` |
| 1k | **Task nào cũng xong, chỉ báo lên khi thật sự bị chặn** (kế hoạch 2026-10-11): retry có thông tin (`runner/feedback.py`) · model rework mạnh hơn · vá transition (question mọi route, VERIFY blocked/escalation, G1 hết giờ, merge conflict lên base mới) · sửa spec giữa chừng qua `spec_patch` (tự duyệt khi an toàn) · preflight lệnh spec + baseline đỏ sẵn · thang phục hồi 0 LLM (`runner/recovery.py`) · `blocked_report.json` · `ddt reopen` · tra cứu `web.search` / `web.fetch` / `docs.lookup` theo allowlist · `llm.ask` thật (quick / second_opinion) · `skill.search` / `skill.load` | **Phase 1–4 xong** — `tests/unit/runner/test_retry_feedback.py`, `test_spec_amend.py`, `test_recovery.py`, `tests/unit/tools/test_web.py`; còn Phase 5–11 (loại task, đa ngôn ngữ, Learner, SPLIT, MCP client) |
| 2 | Docker sandbox · condenser tầng 2–3 · Gemini/Bedrock | Khung sẵn (Protocol + stub `NOT_IMPLEMENTED` có hint) |
| 3 | Memory 3 loại / KB 5 tầng · Learner · learned skill/checklist · delegation subagent · canary · retro | Phần tất định đã chạy: episode, worker_stats, standup, perf review (1g), fact KB vào brief, canary Verifier, retro mỗi 10 issue (1j). Còn khung: Learner LLM, retrieval episode vào brief (`memory/`, `tools/agent.*`) |

## Thi đấu MTM 2026 (Codex CLI · 40 phút · không người)

```powershell
# Tạo profile, cấu hình bridge/auth theo runbook trước các lệnh dưới.
codex login  # tài khoản Codex BTC cấp
ddt --repo $teamRepo --home $contestHome --provider codex match prepare --match $matchId
ddt --repo $teamRepo --home $contestHome --provider codex match run --match $matchId --slots 1
ddt --repo $teamRepo --home $contestHome match status
# Kiểm tra xuyên suốt bằng model giả, Git remote tạm và Grader chạy test ẩn:
python -m pytest tests/unit/runner/test_match.py::test_e2e_match_bug_is_fixed_pushed_submitted_and_graded -q
ddt --match debug footprint  # token cố định theo vai, không phải chi phí credit chính thức
```

- **Codex** (`llm/codex_cli_provider.py`, `--provider codex` = preset `codex_all`): mỗi agent run một `codex exec --json`;
  shell/plugin của Codex tắt, sandbox read-only, không nạp AGENTS.md repo đề → mọi tool đi qua MCP DDT (policy + log).
- **`--match`** (`match run` tự bật): autonomy `autonomous` — cổng mở ticket để lưu vết nhưng harness quyết ngay; GATE/REVIEW
  theo rule tới T1; tắt Learner, canary, SPLIT; gỡ tool không ra điểm (`policy.tool_denylist`).
- **Vòng trận** (`runner/match_runner.py`): đọc trang match mỗi `gamemaster.poll_s` · item mới → issue · đề đổi chưa nộp →
  phiên bản mới khi chưa giữ request nộp · CLOSED → huỷ · xếp lịch PI → sắp hết hạn → điểm/phút · 1 slot + 1 slot PI mặc định ·
  INGEST gọi Start Working (MVS vào `match.json`) · MERGE push rồi nộp (`gitRevision` đủ 40 ký tự, Idempotency-Key cố định).
- Hạn item: `match.json#deadline_at` (giờ server quy đổi) − `deadline_margin_s`; quá hạn → bỏ item, không phục hồi.
- PAUSED hủy hợp tác worker, dừng mutation; FINISHED tiếp tục hỏi grade trong grace period. Ledger SQLite giữ nguyên body/key qua restart.

## Cài đặt

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,openai,win]"
ddt --version
```

Yêu cầu: Python 3.12 · git ≥ 2.40 · (tuỳ chọn) ripgrep cho `fs.search` nhanh. Không cần Docker. Không cần API key để chạy test.

## Kiểm nhanh

```powershell
ddt config check          # config đóng gói hợp lệ, mọi role resolve được model
ddt config roles          # role → provider/model thật sự dùng, có key hay chưa
ddt schema check          # 10 JSON Schema draft-07, example pass
pytest -q                 # ~2 phút, không Docker, không API key (-m live cần ANTHROPIC_API_KEY)
```

## Chạy một issue trên repo mẫu

```powershell
$repo = "testdata/repos/mini_calc"      # repo git thật (git init + commit trước nếu copy ra ngoài)
ddt --repo $repo ingest testdata/backlogs/calc.csv            # backlog → item.json + queue
ddt --repo $repo --provider mock run --next --golden testdata/golden/calc_mean
#   → ... conf 0.77 → T2 → chờ G1 (exit 4)
ddt --repo $repo gates                                        # gate đang mở
ddt --repo $repo resolve DDT-0001 G1 --approve --by human:P1
ddt --repo $repo --provider mock run DDT-0001 --golden testdata/golden/calc_mean
#   → MERGE → DONE (exit 0): main có commit ddt(DDT-0001), worktree dọn
ddt --repo $repo replay DDT-0001                              # tua lại từng event
ddt --repo $repo report                                       # 1 trang
```

Với provider thật: bỏ `--provider mock --golden`, đặt `ANTHROPIC_API_KEY` (hoặc preset khác qua `models.yaml`).

### Chạy bằng Claude Code CLI (subscription, không cần API key)

```powershell
# cần `claude` trong PATH và đã đăng nhập (chạy `claude` một lần). Kiểm: ddt --provider cli config roles
Copy-Item -Recurse testdata/repos/shop_api $env:TEMP/shop_api; git -C $env:TEMP/shop_api init -b main; git -C $env:TEMP/shop_api add -A; git -C $env:TEMP/shop_api commit -qm init
ddt --repo $env:TEMP/shop_api --provider cli ingest testdata/backlogs/shop.csv
ddt --repo $env:TEMP/shop_api --provider cli run DDT-0001        # TRIAGE…GATE qua CLI → chờ G1 (exit 4)
ddt --repo $env:TEMP/shop_api resolve DDT-0001 G1 --approve
ddt --repo $env:TEMP/shop_api --provider cli run DDT-0001        # MERGE → DONE
```

`--provider cli` = preset `cli_all` (mọi role qua CLI). Có key API thì dùng `--preset cli_build` (chỉ Dev đi CLI, role 1 call đi API, hết hạn mức → fallback API cùng issue).
Cách chạy: role một call → `claude -p --output-format json` (không tool); role có loop (planner/dev/verifier/security) → `--output-format stream-json`, loop nằm trong CLI, **mọi tool** qua MCP server `ddt mcp --session` → cùng Dispatcher/policy/allowlist, `events.jsonl` chung (khoá file liên process). Cờ an toàn cố định: `--tools ""` (tắt built-in), `--strict-mcp-config`, `--setting-sources user` (không nạp CLAUDE.md repo đích), `--permission-prompt-tool mcp__ddt__policy`, không bao giờ `--dangerously-skip-permissions`. Budget vẫn do harness đếm (CLI 2.1 không có `--max-turns`): chạm trần → kill cây process; `budgets.yaml#transport_overhead.cli` cộng phụ phí system prompt của Claude Code vào trần stage.
Thư mục làm việc (DDT home) mặc định `<repo-parent>/<repo>.ddt/` — **ngoài** repo đích (`--home` để đổi).

### Worker

```powershell
ddt --repo $repo ingest testdata/backlogs/calc.csv
ddt --repo $repo --provider mock worker --golden testdata/golden/calc_mean   # chạy pipeline liên tục
ddt --repo $repo watch                                                       # terminal khác: theo dõi events
```

`ddt worker` là process DUY NHẤT chạy pipeline (heartbeat vào bảng `worker_heartbeat`). Lệnh khác chỉ ghi `gate_requests`, `state/STOP`, hàng lệnh `control_requests`; worker áp ở đầu tick kế. STOP → thread watcher cancel token của stage đang chạy (< 5s), checkpoint giữ, Resume tiếp đúng stage. Theo dõi bằng `ddt gates · resolve · watch · board · report`.

### Tool chất lượng (scanner · coverage · code graph)

```powershell
pip install -e .                            # đã kèm bandit · pip-audit · detect-secrets · diff-cover · pytest-cov · pytest-testmon
npm i -g @colbymchenry/codegraph            # TÙY CHỌN: CodeGraph CLI (tree-sitter, ~20 ngôn ngữ); thiếu → codegraph.* dùng PyGraph (ast, Python) tự động
ddt config check                            # cảnh báo binary thiếu → slot tương ứng skipped
ddt --repo $repo debug codegraph-init       # index <repo>/.codegraph/ (exclude khỏi git) — 1 lần lúc onboarding
ddt --repo $repo debug tool DDT-0001 security.scan --role security --args '{"targets":["sast","secrets"]}'
ddt --repo $repo debug tool DDT-0001 codegraph.callers --role planner --args '{"symbol":"reserve"}'
ddt debug skills                            # 12 skill: 7 core + 5 imported (origin · license · token/cap)
```

Mọi asset agent-facing (agent JD, skill, checklist, prompt, mô tả tool/schema) viết bằng **tiếng Anh**; CLI/console/log cho người vận hành bằng tiếng Việt.

MACHINE_GATE giờ có thêm slot `sast` (bandit trên file .py đổi, fail khi có HIGH), `coverage` (pytest-cov + diff-cover so với base, không fail, số đo vào `evidence.machine_gate.coverage_delta/uncovered_new_lines`) và `blast_radius` (ast → symbol đổi → CodeGraph impact → FMEA RPN). Checklist `verify: test` (TEST-004/005, SEC-009/010) chạy trên các số đo này.

### Lệnh khác

| Lệnh | Việc |
|---|---|
| `ddt worker --until-idle` | chạy hết queue rồi thoát (script/demo); `--stub-stages` để kiểm runner không LLM |
| `ddt handover ISSUE [--write]` · `ddt report --write` | biên bản bàn giao (PR description) · báo cáo trận, ghi `<home>/reports/` |
| `ddt perf [--write] [--backfill]` | performance review từ `role_outcomes`; `--backfill` ghi số liệu cho issue đã xong trước khi có tính năng (phiên bản JD ghi 0, không rõ) |
| `ddt security-report ISSUE [--write]` | báo cáo sự cố bảo mật: advisory + 5C từ `sec_report`, policy chặn, gate |
| `ddt runtime show · set policy.autonomy=aggressive · unset · clear` | 3 cần điều khiển, worker nạp lại ở issue kế |
| `ddt run ISSUE --stub-stages` | chạy state machine với stage giả (0 LLM) — demo/kiểm runner |
| `ddt run ISSUE --until SPEC` | dừng sau stage |
| `ddt stop` / `ddt resume [ISSUE]` | G3 kill switch (`state/STOP`) |
| `ddt watch [ISSUE]` · `ddt board` · `ddt queue` | theo dõi |
| `ddt debug brief ISSUE --role dev --system` | xem brief 4 khối + system prompt agent sẽ nhận |
| `ddt debug tool ISSUE fs.read --role dev --args '{"path":"calc.py"}'` | gọi 1 tool qua đúng đường policy |
| `ddt debug write-slot · read-slot · list-slots · reindex · emit · llm-ping · agents` | thao tác blackboard/index/LLM |

### Override config

Mọi ngưỡng là dữ liệu: `policy.yaml` · `budgets.yaml` · `models.yaml` · `sensitive_paths.yaml`. Lớp: đóng gói → `<home>/overrides/config/*.yaml` → `--config-dir DIR/config/*.yaml` (thay nguyên file) → flag (`--minimal`, `--economy`, `--wip N` …). Agent/skill/tool/checklist cũng override được cùng cách (`<home>/overrides/agents/…`).

## Cấu trúc

```
src/digital_development_team/        layer (import chỉ đi xuống — tests/contract/test_architecture.py)
  core/        L0 kiểu dữ liệu, enum, Protocol, lỗi — không I/O
  util/ expr/  L1 fsio · jsonio · yamlio · hashing · paths · globs · proc(subprocess duy nhất) · text · biểu thức an toàn
  config/ eventlog/ storage/   L2 YAML theo lớp + Pydantic + hot-reload · events.jsonl + transcript.py (quá trình agent, redact) · sqlite3 + migration + layout home + heartbeat
  schema/      L3 JSON Schema registry + View + OUTPUT CONTRACT
  blackboard/ llm/ sandbox/ policy/ budget/ risk/ isolation/ memory/ checklist/ gates/ gamemaster/   L4  (llm/codex_cli_provider.py · claude_cli_provider.py = transport CLI · gamemaster/ = Game Master MTM + giả lập)
               memory/outcomes.py = kết quả issue → role_outcomes · episode · worker_stats · standup · familiarity
  tools/ ingest/   L5 catalog YAML + impl fs/exec/test/git/blackboard + dispatcher · backlog CSV/JSON/Game Master + queue
  agents/      L6 spec (.agent.md) · skills · prompt · brief 4 khối · AgentLoop
  runner/ reporting/   L7 state machine · stages (agent/machine_gate/gate/merge/gate_wait/parallel/stub) · worker (vòng lặp + StopWatcher + dựng lại runtime khi override đổi) · report · stats theo role · handover · perf
  app.py       L8 composition root (build_runtime · services · build_tooling)
  cli/         L9 typer (+ `ddt mcp` — MCP server stdio cho transport CLI · `ddt worker`)
  _assets/     config · schemas · agents · skills/{core,imported} · tools · checklists · prompts  (đóng gói, override được)
tests/         unit · contract · integration · live    testdata/   repos/{mini_calc, shop_api} · backlogs/{calc,shop,…}.csv · golden/calc_mean
```

## Nguyên tắc không đổi

1. Agent chỉ nói chuyện qua **blackboard**; mọi artifact qua **JSON Schema** trước khi ghi; artifact **bất biến** (version mới).
2. **Verifier độc lập**: chỉ thấy spec + diff, không memory, khác họ model với Dev nếu có key.
3. **Policy là dữ liệu**: tool allowlist cố định theo frontmatter; 4 lớp hooks → rules → permission → human; không tool nào push.
4. **Harness quyết điểm phát hành**: confidence tính từ tín hiệu đo được (max 0.90); tier chỉ tăng; G1 cưỡng chế theo `gates.g1.requires_human_when`, không tin Leader.
5. **Mọi side effect có provenance** trong `events.jsonl` (seq liền, fsync) → `ddt replay`.

## Quyết định kiến trúc

| # | Quyết định | Vì sao |
|---|---|---|
| 1 | **Sync + `ThreadPoolExecutor`** tại đúng 3 điểm (worker pool, VERIFY∥REVIEW, subagent), một `CancelToken` xuyên tầng; asyncio chỉ trong MCP server | Một "màu" hàm; `sqlite3`/`subprocess`/`fsync` dùng thẳng; stack trace thẳng |
| 2 | **`sqlite3` stdlib** + wrapper mỏng, migration `NNNN_*.sql` + `PRAGMA user_version`; không ORM | 6 bảng + FTS5 + `BEGIN IMMEDIATE` đằng nào cũng SQL thô; tránh bẫy pool/`check_same_thread` |
| 3 | **`ddt.expr`** tự viết: `ast.parse(mode="eval")` + whitelist node, compile lúc load config; **không `eval`**; thiếu giá trị = lỗi (fail closed) | Rule trong YAML an toàn, lỗi lộ ở `config check` chứ không giữa issue |
| 4 | **Artifact: JSON Schema là nguồn sự thật**, Pydantic chỉ là View đọc; **Config: Pydantic** là nguồn sự thật | Không hai nguồn cùng sở hữu một hợp đồng; contract test giữ đồng bộ |
| 5 | **DDT home ngoài repo đích** (`<repo-parent>/<repo>.ddt/`) | pytest/rg trong repo không quét worktree; tránh MAX_PATH; repo đích sạch |
| 6 | **Python 3.12** thay Go (đổi 2026-10-03) | SDK LLM chính thức đầy đủ, đội quen Python, `pip install -e .` là xong |
| 7 | **Transport CLI** (Claude Code) song song API qua `LLMProvider` hai mức; mọi tool của CLI đi qua MCP server DDT | Chạy bằng subscription không cần key, nhưng policy/allowlist/event log vẫn là một đường duy nhất |
| 10 | **Transcript tách khỏi events.jsonl**: events giữ hash/ref (audit tối giản, một writer); transcript `transcripts/*.jsonl` ghi brief, text assistant, tool args/kết quả đã `redact_secrets`, tắt bằng `policy.transcripts=false` / `--no-transcripts` | Người cần thấy "worker đã làm gì" mà không phá nguyên tắc event log không chứa prompt |
| 9 | **Timeout gate = 0 nghĩa là không timeout** (`_positive()` trong gate_wait); `proceed_with_recommendation` cần cả Verifier lẫn Security pass | 0 từng bị hiểu là "hết hạn ngay" → T0/T1 conf thấp tự merge không ai duyệt |
| 11 | **Số liệu học hỏi tất định, ghi lúc issue kết thúc**: runner chấm pass từng role từ checkpoint + events + artifact theo định nghĩa trong 05-profiles, không hỏi LLM; idempotent theo issue; lỗi ở bước này chỉ ghi log. Worker so dấu vân tay config và lớp override trước mỗi issue, đổi thì dựng lại runner (registry JD/skill/checklist, model) và emit `config_reloaded` | Sửa JD qua override phải có hiệu lực thật ở issue kế và phải đo được trước/sau, nếu không A/B prompt chỉ là cảm giác |
| 12 | **Luật chuyển trạng thái đọc artifact thật**: tên slot (`spec`, `intake`…) trong biểu thức trả nội dung slot trên blackboard trước, facts chỉ là dự phòng. Kết quả `escalation` và `blocked` của agent có luật riêng ở mọi stage agent | Facts chỉ giữ trường kiểu đơn; đọc facts trước làm `len(spec.ambiguities)` luôn bằng 0 và escalation bị coi như đã có artifact |
| 13 | **Kiểm lệnh shell có cấu trúc ở Lớp 1, không regex trên cả chuỗi**: LocalSandbox không chặn mạng hay ghi ngoài worktree nên đây là lớp chặn chính khi chưa có Docker; áp cho exec.run, test.run và cả lệnh gate do agent viết | Regex cũ bị lách bằng `git -c x=y push`, `curl''`, `python -c "subprocess…"`, đi vòng qua test.run, và chặn nhầm `rsync`, `test_dockerfile.py`. Docker vẫn là ranh giới thật cho pilot |

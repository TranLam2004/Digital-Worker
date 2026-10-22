# DDT × Codex — vận hành thi MTM

Cập nhật 17/10/2026. Căn cứ đề người dùng cung cấp và mã trong repository này.

**Đã kiểm chứng luồng thi bằng mô phỏng. Chưa chứng nhận kết nối BTC hoặc quyền truy cập model của tài khoản thi.**
Game Player, giao thức/auth, template và thao tác READY chính thức cần được đối chiếu khi BTC cung cấp.
Không dùng các đường dẫn HTTP mặc định của simulator như thể đó là API thật.

## 1. Môi trường đã chuẩn bị

Trong thư mục `Digital_Development_Team` có `.venv-codex`, đã cài dự án và công cụ kiểm thử.
MCP được giới hạn `>=1.2,<2` vì DDT đang dùng API server 1.x. Dependency cài thực tế được ghi ở
`requirements-codex-win.lock`; lock này dành cho môi trường Windows đã kiểm chứng.

```powershell
Set-Location 'C:\Users\User\Downloads\Digital_Worker\Digital_Worker\Digital_Development_Team'
$env:PATH = "$PWD\.venv-codex\Scripts;$env:PATH"
$env:PYTHONUTF8 = '1'
ddt --help
codex --version
```

Để dựng lại môi trường: tạo venv Python 3.12+; cài lock rồi `pip install --no-deps -e .`.
Không cài hoặc cập nhật dependency, plugin, skill trong lúc trận đang chạy.

## 2. Hai cấu hình, hai vùng trạng thái riêng

Lệnh `config profile` mới tạo `overrides/config/runtime.yaml` trong `--home` chỉ định.
Lệnh từ chối ghi đè cấu hình đã có. Mỗi trận dùng một home riêng để tránh trộn backlog, checkpoint và bài nộp.

Điền model ID **có thật trong tài khoản Codex của đội**, rồi phân bổ ngân sách còn lại cho trận.
Ba biến model có thể cùng một ID nếu tài khoản chỉ cho một model; effort và vai trò vẫn khác nhau.

```powershell
# Đặt $fastModel, $mainModel, $strongModel theo model picker/tài liệu tài khoản thi.
# Đặt $dailyHome và $contestHome thành hai đường dẫn khác nhau.
# Đặt $dailyBudgetUsd và $roundBudgetUsd theo ngân sách đã phân bổ.
ddt --home $dailyHome config profile daily `
  --fast-model $fastModel --main-model $mainModel --strong-model $strongModel `
  --budget-usd $dailyBudgetUsd

ddt --home $contestHome config profile contest `
  --fast-model $fastModel --main-model $mainModel --strong-model $strongModel `
  --budget-usd $roundBudgetUsd --tokens 1000000
```

| Thiết lập | Daily | Contest |
|---|---|---|
| Điều khiển | Conservative, giữ các cổng người dùng | Autonomous, không hỏi người trong trận |
| Provider | Codex/GPT | Bắt buộc Codex/GPT, kiểm cả fallback |
| Dev lần đầu | Model chính | Model chính |
| Rework/việc khó | Model mạnh | Model mạnh |
| Học sau task | Bật | Tắt |
| Số việc mặc định | WIP 1 | 1 luồng thường + 1 luồng riêng PI |
| Dừng nhận việc mới | 85% trần cục bộ | 85% trần cục bộ |

Trần USD là **ước lượng nội bộ**, không phải khóa chi tiêu tuyệt đối. Codex báo usage khi kết thúc exec;
việc đang chạy còn tiêu thụ ngân sách. Cần đối chiếu credit BTC giữa các vòng và trừ chi phí preflight.
`$3/trận` trong kế hoạch trước chỉ là ví dụ phân bổ, không phải mức đã được đo hoặc BTC bảo đảm.
Model/pricing chưa được xác nhận thì không dùng số USD nội bộ để kết luận còn đủ credit.

## 3. Gắn Game Player do BTC cung cấp

Đường gắn đã có: `policy.gamemaster.adapter_factory = module:function`.
Module là bridge cục bộ do đội kiểm soát, dùng SDK/CLI/transport **được BTC cho phép**.
Factory nhận `config`, `match_id`, `clock`, `repo` và trả object thực hiện
[`GameMaster`](../src/digital_development_team/gamemaster/port.py).

```powershell
ddt --home $contestHome runtime set policy.gamemaster.adapter_factory=team_game_player:create
```

| Hàm bridge | Kết quả chuẩn hóa / nghĩa vụ |
|---|---|
| `snapshot()` | MatchSnapshot có match_id, status, server_time; ends_at nếu kênh chính thức cung cấp; chỉ item được phép xem |
| `start_working(item_id)` | StartResult có submit_template, MVS của BUG/CR; lỗi kết nối phải trả lỗi DDTError |
| `submit(...)` | Gửi full SHA ở root `gitRevision`, answer theo template, giữ nguyên Idempotency-Key khi retry |
| `submission(item_id, submission_id)` | Đọc PENDING/PASSED/FAILED và điểm chính thức, kể cả sau FINISHED |

Bridge phải ánh xạ thao tác và dữ liệu từ tài liệu chính thức; tên hàm trên là interface nội bộ DDT,
không phải tuyên bố về tên API của BTC. READY vẫn thực hiện qua Game Player chính thức trước Start;
`prepare` hiện không tự gửi READY bằng một endpoint suy đoán.

HTTP client có sẵn dùng được cho simulator. Ngoài loopback, nó từ chối chạy nếu chưa bật
`http_contract_confirmed`. Chỉ bật sau khi BTC xác nhận HTTP contract tương ứng.
Bearer chỉ đính kèm các action có tên trong `bearer_actions`; mặc định danh sách rỗng.
Không tự dùng token này để đọc context/state hoặc truy cập Admin/Grader.

## 4. Chuẩn bị trước Start

1. Nhận tài khoản, Game Player, contract và đề đã mở. Kiểm model ID/effort thực tế.
2. Pull repo đội bằng quy trình BTC cho phép; checkout `main`. Source ở `<MatchID>/`.
3. `--repo` trỏ **Git root của đội**, không trỏ vào thư mục con `<MatchID>/`.
4. Dùng home riêng cho trận và cấu hình bridge. `match_folder` mặc định là `{match_id}`.
5. Chạy preflight khi match còn READY/PREPARING:

```powershell
ddt --repo $teamRepo --home $contestHome --provider codex config check
ddt --repo $teamRepo --home $contestHome --provider codex match prepare --match $matchId
```

`prepare` kiểm remote/nhánh/SHA, giới hạn onboarding vào thư mục trận, kiểm toolchain/test command,
và warm up các cấu hình model được chọn. Các ping model có tiêu credit.
Không coi thông báo preflight hoàn tất là xác nhận READY của BTC.
Nếu dùng `--skip-onboard`, đội chịu trách nhiệm đã chuẩn bị REPO.md và lệnh test trước đó.

## 5. Chạy trận

```powershell
ddt --repo $teamRepo --home $contestHome --provider codex match run --match $matchId --slots 1
```

Lệnh có thể chờ READY/PREPARING rồi tự nhận việc khi IN_PROGRESS. Trong trận không đổi config/model/skill,
không tự cập nhật DDT và không thao tác thủ công nếu chưa chấp nhận mức phạt theo luật.

Luồng thực hiện:

`Snapshot → ưu tiên PI → Start Working → SPEC/MVS → code → test → verifier → final tests → merge → push → submit → grade`

- Start Working lỗi: giữ checkpoint INGEST, backoff; không chạy agent thiếu MVS/template.
- MVS phải có `mvs_ids`, `test_ref`, `test_command`; bằng chứng từng AC phải pass, có test output.
- Sau rebase, chạy lại các lệnh MVS, test chính và regression trên cây cuối. Gắn SHA và hash spec/evidence.
- Push thiếu remote hoặc lỗi: không nộp. Kiểm SHA được nhánh remote chứa trước khi gửi.
- Lưu body/key vào SQLite theo `(match_id,item_id)` **trước gửi**. Timeout giữ UNKNOWN, retry đúng body/key.
- ACCEPTED dừng mọi nộp mới cho item, dù grade cuối là FAILED. ACCEPTED không đồng nghĩa PASSED.
- PAUSED hủy hợp tác các worker và dừng dispatch/submit; tiếp tục từ checkpoint khi IN_PROGRESS.
- Snapshot lỗi quá 30 giây: hủy hợp tác việc đang chạy; không cho mutation khi chưa xác nhận trạng thái hiện tại.
- FINISHED/CANCELLED: ngừng làm/nộp, tiếp tục đọc grade tối đa 300 giây. Hết grace vẫn PENDING thì giữ PENDING.

```powershell
# Xem trạng thái lưu cục bộ; tuân thủ quy định thao tác của BTC trong trận.
ddt --repo $teamRepo --home $contestHome match status
```

## 6. Kiểm chứng trước khi dùng tài khoản thật

```powershell
python -m pytest tests/unit/gamemaster tests/unit/storage/test_submissions.py `
  tests/unit/runner/test_match.py tests/unit/runner/test_contest_validation.py `
  tests/unit/runner/test_contest_preflight.py tests/unit/llm/test_codex_cli.py `
  tests/unit/cli/test_mcp_serve.py -q
```

Test E2E tạo repo/remote tạm, dùng transcript model giả, chạy test thật, push thật vào remote tạm,
nộp qua FakeGameMaster rồi Grader giả chạy test ẩn trên SHA. Không dùng credit LLM hoặc server BTC.
Luyện tập này chứng minh cơ chế harness; không dự đoán điểm bài thi, độ đúng của model hay độ trễ mạng thực tế.

## 7. Codex, skill và plugin

Codex CLI chạy vòng agent; DDT cung cấp công cụ qua MCP theo quyền từng vai trò. Skill DDT được chọn theo task
và đưa vào brief. `beyond-mvs` đã cập nhật để sinh mapping mà harness kiểm được.
Đây không phải tự động cài mọi skill DDT thành skill cá nhân Codex.

Chế độ thi giữ tập công cụ gọn và vô hiệu plugin cá nhân trong Codex child process; tránh thêm schema/tool không dùng
vào mỗi lượt. Game Player đặt ở tầng harness, không cấp token/API submit cho agent.
Plugin Codex phục vụ chuẩn bị hoặc công việc hằng ngày có thể cấu hình riêng ngoài contest home.
Chỉ thêm plugin vào đường thi sau khi có nhu cầu từ đề và rehearsal chứng minh lợi ích thời gian/chi phí.

## 8. Việc còn phụ thuộc BTC

- Implement và contract-test bridge với Game Player chính thức, bao gồm READY, auth, pagination/version nếu có.
- Đối chiếu submit template thật. Trường lạ, boolean/số mẫu hoặc cấu trúc chưa hỗ trợ hiện bị chặn;
  phải ánh xạ có kiểm chứng, không điền đại bằng summary.
- Kiểm model account, giá/credit và đo p50/p95 thời gian với bài thử tương tự đề thật.
- Chạy rehearsal 40 phút với tài khoản/transport/toolchain chính thức rồi mới chốt model, concurrency và budget.

Harness hiện giảm các lỗi vận hành có thể phòng ngừa; các bước trên là điều kiện còn thiếu để xác nhận sẵn sàng thi thật.

# Kết quả triển khai contest readiness — 17/10/2026

Đã chuyển các phần trọng yếu từ kế hoạch sang mã và kiểm thử. Hướng dẫn dùng:
[CONTEST-CODEX-RUNBOOK.vi.md](CONTEST-CODEX-RUNBOOK.vi.md).

| Hạng mục | Đã triển khai |
|---|---|
| Parse dữ liệu | Không tự tạo ACCEPTED/PASSED/IN_PROGRESS hoặc lấy giờ máy thay serverTime khi thiếu dữ liệu bắt buộc |
| Auth/transport | Không gửi Bearer tùy tiện; HTTP ngoài loopback cần contract được xác nhận; có factory cho Game Player bridge |
| Start Working | Lỗi → PAUSED tại INGEST, không tiêu lượt agent trên template/MVS chưa nhận |
| Nộp một lần | Migration SQLite 0007; request bất biến theo match/item, dùng chung mọi version; kết quả ACCEPTED/grade cuối không bị timeout ghi đè |
| Chất lượng | Mapping MVS → AC → lệnh test → evidence; kiểm lại sau rebase; SHA và hash spec/evidence gắn với verification |
| Git | Thiếu remote chặn nộp; kiểm commit tồn tại trong nhánh remote; SHA đầy đủ ở root request |
| Lifecycle | Pause/stale snapshot hủy hợp tác; backoff; lấy hạn sớm hơn giữa item và match; đọc grade sau finish có giới hạn |
| Chi phí | Chặn dispatch mới khi đạt trần session cục bộ; mặc định 1 slot thường + 1 PI; profile có model/budget đầu vào tường minh |
| Cấu hình | `ddt --home ... config profile daily|contest`; không ghi đè runtime có sẵn; không dùng lại home cho trận khác |
| Repo đề | Kiểm Git root/main/remote/thư mục trận; onboarding có scope; preflight hiểu `cd <MatchID> && ...` |
| Song song | Mỗi slot CLI có bộ tạo brief và tool stack riêng; Git mutations vẫn dùng khóa của harness |
| Windows/checkpoint | Retry đọc file bị khóa có giới hạn; tên file tạm riêng cho mỗi lần ghi; khóa cập nhật match.json giữa scheduler, worker và grader |
| Codex/MCP | Khóa MCP 1.x phù hợp server hiện tại; kiểm thử CLI adapter và server MCP bằng fixture |

## Bằng chứng kiểm chứng

- Full suite trên mã cuối cùng: **851 passed, 2 skipped, 2 deselected** (299,50 giây).
- Bao gồm luồng daily, contest, preflight thư mục trận, retry checkpoint Windows và cập nhật trạng thái đồng thời.
- Ruff: **All checks passed**; mypy: **không có lỗi trong 233 source files**; `pip check`: **không có dependency bị hỏng**.
- E2E đã chạy: BUG → sửa code → MVS/regression → merge/push → ACCEPTED → PASSED từ test ẩn của simulator.
- Hai skip của full suite: thiếu Go và CodeGraph CLI. Hai test live bị loại theo cấu hình mặc định.
- Không gọi LLM trả phí, không liên hệ server BTC, không push repository người dùng hoặc gửi bài thật.

Chạy lại từ Git root của DDT:

```powershell
$env:PATH = "$PWD\.venv-codex\Scripts;$env:PATH"
$env:PYTHONUTF8 = '1'
python -m pytest -q --tb=short
python -m ruff check src tests
python -m mypy src
python -m pip check
```

Kết quả này không thay thế contract test với BTC. API/READY/auth/template/model chính thức chưa có trong workspace.
Ngân sách USD nội bộ không bảo đảm khớp credit thực của Codex; cần đối soát và đo thử với tài khoản thi.
Việc hủy worker là hợp tác qua CancelToken, không phải cơ chế dừng giao dịch đang bay trên mạng một cách tức thời.

Thay đổi có sẵn của người dùng trong `skills/imported/py-review/SKILL.md` được giữ nguyên.

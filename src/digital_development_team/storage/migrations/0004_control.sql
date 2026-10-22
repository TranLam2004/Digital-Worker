-- 0004_control — lệnh người gửi từ web cho worker thực thi (nạp backlog, đổi ưu tiên, huỷ, chạy lại, dừng worker…).
-- Web không ghi events.jsonl hay checkpoint: chỉ ghi dòng ở đây, worker (writer duy nhất) áp ở tick kế và ghi kết quả.
CREATE TABLE control_requests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,
  issue_id      TEXT,
  payload       TEXT NOT NULL DEFAULT '{}',
  requested_by  TEXT NOT NULL,
  requested_at  TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending · done · failed
  result        TEXT,
  done_at       TEXT
);
CREATE INDEX idx_control_status ON control_requests(status, id);

-- Người đánh dấu một issue đã merge là sai (đo "merge tự động sai", dạy hệ thống qua human_correction).
CREATE TABLE merge_feedback (
  issue_id     TEXT PRIMARY KEY,
  by_actor     TEXT NOT NULL,
  note         TEXT NOT NULL,
  auto_merged  INTEGER NOT NULL,     -- 1 = merge không qua người duyệt G1
  at           TEXT NOT NULL
);

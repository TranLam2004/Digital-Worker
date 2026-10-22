-- 0003_outcomes — một dòng mỗi (issue, role) khi issue kết thúc (done · blocked). Bảng sự thật cho hồ sơ năng lực,
-- so sánh phiên bản JD (A/B), module khó, learning curve. worker_stats (0001) là bảng tổng hợp tính lại từ đây.
-- Ghi bởi runner lúc kết thúc issue (memory/outcomes.py), 0 LLM, idempotent theo issue_id.
CREATE TABLE role_outcomes (
  issue_id      TEXT NOT NULL,
  role          TEXT NOT NULL,
  agent_version INTEGER NOT NULL DEFAULT 1,
  issue_type    TEXT NOT NULL,
  module        TEXT NOT NULL,
  tier          TEXT NOT NULL,
  outcome       TEXT NOT NULL,             -- done · blocked
  passed        INTEGER,                   -- 1 · 0 · NULL (không đo được với issue này)
  reason        TEXT,                      -- vì sao pass/fail theo định nghĩa của role
  tokens        INTEGER NOT NULL DEFAULT 0,
  cost_usd      REAL NOT NULL DEFAULT 0,
  calls         INTEGER NOT NULL DEFAULT 0,
  model         TEXT,
  seq_no        INTEGER NOT NULL,          -- thứ tự kết thúc trong home (learning curve)
  finished_at   TEXT NOT NULL,
  PRIMARY KEY (issue_id, role)
);
CREATE INDEX idx_ro_role ON role_outcomes(role, agent_version);
CREATE INDEX idx_ro_module ON role_outcomes(module, role);
CREATE INDEX idx_ro_seq ON role_outcomes(seq_no);

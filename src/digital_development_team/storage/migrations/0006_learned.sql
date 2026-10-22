-- 0006_learned — skill học được (quarantine → candidate → active → retired) và hàng việc của Learner.
-- Nội dung skill nằm trên fs (memory/skills/<name>/v<N>.SKILL.md); bảng giữ trạng thái + số lần dùng (không dựng lại được).

CREATE TABLE learned_skills (
  name          TEXT PRIMARY KEY,          -- learned-<slug>
  version       INTEGER NOT NULL,
  status        TEXT NOT NULL,             -- quarantine · candidate · active · retired · rejected
  sha256        TEXT NOT NULL,
  description   TEXT NOT NULL DEFAULT '',
  module        TEXT NOT NULL DEFAULT '*',
  issue_type    TEXT NOT NULL DEFAULT '*',
  used_by       TEXT NOT NULL DEFAULT '["dev"]',   -- JSON
  confidence    REAL NOT NULL DEFAULT 0.5,
  applied       INTEGER NOT NULL DEFAULT 0,  -- số issue đã nhận skill (bản hiện tại) và đo được kết quả
  passed        INTEGER NOT NULL DEFAULT 0,
  problems      TEXT NOT NULL DEFAULT '[]',  -- JSON: lý do quét không qua (quarantine)
  source_issues TEXT NOT NULL DEFAULT '[]',  -- JSON
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX idx_ls_status ON learned_skills(status, module);

CREATE TABLE skill_events (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  version   INTEGER NOT NULL,
  event     TEXT NOT NULL,                 -- proposed · quarantined · candidate · promoted · used · approved · rejected · retired
  by_actor  TEXT NOT NULL,
  issue_id  TEXT,
  note      TEXT,
  at        TEXT NOT NULL
);
CREATE INDEX idx_se_name ON skill_events(name, id);

-- Issue đáng học (runner/learn.py): Learner chạy khi worker rảnh, không nằm trên đường chính.
CREATE TABLE learn_jobs (
  issue_id   TEXT PRIMARY KEY,
  reasons    TEXT NOT NULL,                -- JSON
  status     TEXT NOT NULL,                -- pending · running · done · failed · skipped
  attempts   INTEGER NOT NULL DEFAULT 0,
  note       TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_lj_status ON learn_jobs(status, created_at);

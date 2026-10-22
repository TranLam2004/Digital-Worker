-- 0001_init — index blackboard, counter event, lock, gate, queue, board, memory.
-- Filesystem là nguồn sự thật; mọi bảng ở đây dựng lại được (ddt debug reindex) trừ episodes/worker_stats.

-- Index artifact. Nội dung nằm trên fs (work/<issue>/<slot>.v<N>.<ext>).
CREATE TABLE slots (
  issue_id    TEXT    NOT NULL,
  slot        TEXT    NOT NULL,            -- 'intake.yaml', 'patch/', ...
  version     INTEGER NOT NULL,
  path        TEXT    NOT NULL,            -- POSIX tương đối home
  sha256      TEXT    NOT NULL,
  written_by  TEXT    NOT NULL,            -- actor: intake · dev · runner · human:P1 · dev:sub1
  written_at  TEXT    NOT NULL,            -- ISO-8601 UTC
  schema_ok   INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (issue_id, slot, version)
);
CREATE INDEX idx_slots_current ON slots(issue_id, slot, version DESC);

-- Counter seq per issue cho events.jsonl (issue_id 'global' cho state/events.global.jsonl).
CREATE TABLE event_seq (
  issue_id TEXT PRIMARY KEY,
  next     INTEGER NOT NULL
);

-- Trạng thái mọi issue — dashboard/CLI đọc. Nguồn: checkpoint.json.
CREATE TABLE board (
  issue_id   TEXT PRIMARY KEY,
  status     TEXT NOT NULL,                -- running · waiting_gate · paused · blocked · done
  stage      TEXT NOT NULL,
  tier       TEXT NOT NULL,
  rework     INTEGER NOT NULL DEFAULT 0,
  attempt    INTEGER NOT NULL DEFAULT 1,
  title      TEXT,
  updated_at TEXT NOT NULL
);

-- Hàng đợi backlog (ingest ghi, runner lấy).
CREATE TABLE queue (
  issue_id    TEXT PRIMARY KEY,
  priority    INTEGER NOT NULL DEFAULT 50, -- thấp = trước
  status      TEXT NOT NULL DEFAULT 'queued', -- queued · running · done · blocked · superseded
  source      TEXT NOT NULL,
  source_id   TEXT NOT NULL,
  title       TEXT NOT NULL,
  blocked_by  TEXT NOT NULL DEFAULT '[]',  -- JSON array issue_id
  enqueued_at TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT
);
CREATE INDEX idx_queue_status ON queue(status, priority, enqueued_at);
CREATE UNIQUE INDEX idx_queue_source ON queue(source, source_id);

-- ChangeIntent lock (07-isolation). P2#21.
CREATE TABLE locks (
  path        TEXT PRIMARY KEY,            -- repo path POSIX
  holder      TEXT NOT NULL,               -- issue_id
  mode        TEXT NOT NULL,               -- soft · exclusive
  base_commit TEXT NOT NULL,
  since       TEXT NOT NULL,
  lease_until TEXT NOT NULL
);

-- Gate chờ người. CLI/web ghi vào đây; chỉ runner emit event (ADR: 1 writer events.jsonl).
CREATE TABLE gate_requests (
  id             TEXT PRIMARY KEY,         -- <issue>:<gate>:<attempt>
  issue_id       TEXT NOT NULL,
  gate           TEXT NOT NULL,            -- G0 · G1 · G2 · G3
  attempt        INTEGER NOT NULL,
  tier           TEXT NOT NULL,
  summary        TEXT NOT NULL,            -- JSON cho người đọc trong 15s
  escalation_uri TEXT,
  opened_at      TEXT NOT NULL,
  timeout_s      REAL,
  status         TEXT NOT NULL DEFAULT 'open', -- open · resolved · timeout
  choice         TEXT,                     -- approve · reject · reject_final · A · B · …
  decided_by     TEXT,
  note           TEXT,
  resolved_at    TEXT
);
CREATE INDEX idx_gate_open ON gate_requests(status, opened_at);

-- Episodic memory (50-knowledge/03). P3. Giữ vĩnh viễn.
CREATE TABLE episodes (
  id               INTEGER PRIMARY KEY,
  issue_id         TEXT NOT NULL,
  issue_type       TEXT NOT NULL,
  module           TEXT NOT NULL,
  files            TEXT NOT NULL,          -- JSON array
  approach         TEXT NOT NULL,
  outcome          TEXT NOT NULL,          -- pass · fail · blocked · superseded
  rework_count     INTEGER NOT NULL DEFAULT 0,
  tokens           INTEGER NOT NULL DEFAULT 0,
  wall_sec         INTEGER NOT NULL DEFAULT 0,
  failure_reason   TEXT,
  lesson           TEXT,
  human_correction INTEGER NOT NULL DEFAULT 0,
  confidence       REAL NOT NULL DEFAULT 0.5,
  evidence_count   INTEGER NOT NULL DEFAULT 1,
  created_at       TEXT NOT NULL
);
CREATE INDEX idx_ep_module ON episodes(module, issue_type);

CREATE VIRTUAL TABLE episodes_fts USING fts5(
  approach, lesson, failure_reason, files,
  content='episodes', content_rowid='id'
);
CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, approach, lesson, failure_reason, files)
  VALUES (new.id, new.approach, new.lesson, new.failure_reason, new.files);
END;
CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, approach, lesson, failure_reason, files)
  VALUES ('delete', old.id, old.approach, old.lesson, old.failure_reason, old.files);
END;
CREATE TRIGGER episodes_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, approach, lesson, failure_reason, files)
  VALUES ('delete', old.id, old.approach, old.lesson, old.failure_reason, old.files);
  INSERT INTO episodes_fts(rowid, approach, lesson, failure_reason, files)
  VALUES (new.id, new.approach, new.lesson, new.failure_reason, new.files);
END;

-- Worker profile (50-knowledge/05). P3.
CREATE TABLE worker_stats (
  role       TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  module     TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  passes     INTEGER NOT NULL DEFAULT 0,
  tokens_sum INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (role, issue_type, module)
);

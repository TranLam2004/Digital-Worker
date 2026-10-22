-- 0002_web — bảng phục vụ `ddt worker` và dashboard `ddt serve`.
-- Dựng lại được: heartbeat là trạng thái tạm; gate_witness là audit (giữ).

-- Nhịp tim của process worker. Web coi worker chết khi now - ts > 3 × poll_s.
CREATE TABLE worker_heartbeat (
  worker_id     TEXT PRIMARY KEY,            -- <host>:<pid>
  pid           INTEGER NOT NULL,
  host          TEXT NOT NULL,
  started_at    TEXT NOT NULL,               -- ISO-8601 UTC
  ts            TEXT NOT NULL,               -- lần beat gần nhất
  state         TEXT NOT NULL,               -- running · idle · stopped · exited
  current_issue TEXT,
  current_stage TEXT,
  loop_n        INTEGER NOT NULL DEFAULT 0,
  poll_s        REAL NOT NULL DEFAULT 2.0
);

-- T3 cần 1 approve + 1 witness (người khác). Sidecar của gate_requests, không sửa bảng gốc.
CREATE TABLE gate_witness (
  ticket_id TEXT NOT NULL,
  actor     TEXT NOT NULL,                   -- human:P1 · human:P2 · human:P3
  action    TEXT NOT NULL,                   -- approve · ack
  at        TEXT NOT NULL,
  note      TEXT,
  PRIMARY KEY (ticket_id, actor, action)
);

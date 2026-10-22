-- 0005_learning — số đo cho canary của Verifier và lift của skill học được / memory (50-knowledge/05-profiles).

-- Mỗi lần harness tiêm lỗi nhân tạo trước khi đưa Verifier (runner/stages/canary.py).
CREATE TABLE canary_results (
  issue_id  TEXT NOT NULL,
  attempt   INTEGER NOT NULL,
  mutation  TEXT NOT NULL,       -- flip_condition · off_by_one · remove_null_check · swap_return · delete_assert
  file      TEXT NOT NULL,
  line      INTEGER,
  caught    INTEGER NOT NULL,    -- 1 = Verifier không cho pass bản có lỗi tiêm
  verdict   TEXT,
  at        TEXT NOT NULL,
  PRIMARY KEY (issue_id, attempt)
);

-- Role đã dùng gì trong brief: so pass có/không có skill học được (skill_lift), có/không memory (memory_lift).
ALTER TABLE role_outcomes ADD COLUMN learned_skills INTEGER NOT NULL DEFAULT 0;
ALTER TABLE role_outcomes ADD COLUMN memory_refs INTEGER NOT NULL DEFAULT 0;
ALTER TABLE role_outcomes ADD COLUMN no_learn INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS match_submissions (
    match_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (match_id, item_id)
);

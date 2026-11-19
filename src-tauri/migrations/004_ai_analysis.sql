BEGIN IMMEDIATE;

-- Non-secret application settings. The Gemini API key is deliberately NOT here:
-- it lives in the Windows Credential Manager only.
CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Provenance for every cloud assessment. Kept separate from the local evidence
-- so the local detector's observations are never overwritten.
CREATE TABLE ai_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    candidate_id INTEGER,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    is_highlight INTEGER NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'other',
    interest_score INTEGER NOT NULL,
    peak_ms INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    mostly_menu INTEGER,
    visually_clear INTEGER,
    focus_x REAL,
    focus_y REAL,
    focus_confidence REAL NOT NULL DEFAULT 0,
    combined_score INTEGER,
    origin TEXT NOT NULL DEFAULT 'candidate',
    model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX ai_assessments_job_idx ON ai_assessments(job_id);

ALTER TABLE jobs ADD COLUMN ai_status TEXT NOT NULL DEFAULT 'idle';
ALTER TABLE jobs ADD COLUMN ai_error TEXT;
ALTER TABLE jobs ADD COLUMN ai_model TEXT;
ALTER TABLE jobs ADD COLUMN ai_windows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN ai_message TEXT;

ALTER TABLE candidates ADD COLUMN origin TEXT NOT NULL DEFAULT 'local';
ALTER TABLE candidates ADD COLUMN ai_title TEXT;
ALTER TABLE candidates ADD COLUMN ai_event_type TEXT;
ALTER TABLE candidates ADD COLUMN ai_reason TEXT;
ALTER TABLE candidates ADD COLUMN ai_score INTEGER;
ALTER TABLE candidates ADD COLUMN ai_peak_ms INTEGER;
ALTER TABLE candidates ADD COLUMN ai_focus_x REAL;
ALTER TABLE candidates ADD COLUMN ai_focus_y REAL;
ALTER TABLE candidates ADD COLUMN combined_score INTEGER;

PRAGMA user_version = 4;
COMMIT;

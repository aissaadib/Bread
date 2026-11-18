BEGIN IMMEDIATE;

CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    width INTEGER,
    height INTEGER,
    frame_rate TEXT,
    video_codec TEXT,
    audio_codec TEXT,
    audio_tracks INTEGER NOT NULL DEFAULT 0,
    modified_unix_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('queued','running','cancelling','completed','failed','cancelled')),
    stage TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    analysis_version TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    notices_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX jobs_created_at_idx ON jobs(created_at DESC);
CREATE INDEX jobs_source_id_idx ON jobs(source_id);

CREATE TABLE candidates (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    peak_ms INTEGER NOT NULL CHECK (peak_ms >= start_ms),
    end_ms INTEGER NOT NULL CHECK (end_ms >= peak_ms),
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    signals_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    UNIQUE(job_id, peak_ms)
);

CREATE INDEX candidates_job_start_idx ON candidates(job_id, start_ms);
PRAGMA user_version = 1;
COMMIT;

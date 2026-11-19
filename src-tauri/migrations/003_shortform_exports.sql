BEGIN IMMEDIATE;

ALTER TABLE jobs ADD COLUMN transcript_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE clip_exports (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL,
    title TEXT NOT NULL,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    peak_ms INTEGER NOT NULL CHECK (peak_ms >= 0),
    aspect TEXT NOT NULL CHECK (aspect IN ('vertical','square','original')),
    mode TEXT NOT NULL CHECK (mode IN ('full_frame','crop','original')),
    crop_x REAL NOT NULL CHECK (crop_x >= 0 AND crop_x <= 1),
    crop_y REAL NOT NULL CHECK (crop_y >= 0 AND crop_y <= 1),
    captions INTEGER NOT NULL DEFAULT 0 CHECK (captions IN (0,1)),
    crf INTEGER NOT NULL DEFAULT 21 CHECK (crf >= 14 AND crf <= 28),
    output_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','preparing','rendering','finalizing','cancelling','completed','failed','cancelled')),
    stage TEXT NOT NULL DEFAULT 'waiting',
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX clip_exports_batch_idx ON clip_exports(batch_id);
CREATE INDEX clip_exports_job_idx ON clip_exports(job_id);
CREATE INDEX clip_exports_created_at_idx ON clip_exports(created_at DESC);

PRAGMA user_version = 3;
COMMIT;

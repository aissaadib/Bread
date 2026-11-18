BEGIN IMMEDIATE;
ALTER TABLE candidates ADD COLUMN title TEXT NOT NULL DEFAULT 'Highlight';
ALTER TABLE candidates ADD COLUMN title_source TEXT NOT NULL DEFAULT 'evidence_fallback';
ALTER TABLE candidates ADD COLUMN clip_path TEXT;
PRAGMA user_version = 2;
COMMIT;

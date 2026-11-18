from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "src-tauri" / "migrations" / "001_initial.sql"


class DatabaseSchemaTests(unittest.TestCase):
    def test_migration_creates_versioned_persistent_tables(self) -> None:
        database_path = ROOT / ".test-output" / "luna-test.sqlite3"
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(SCHEMA.read_text(encoding="utf-8"))
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            source_id = connection.execute(
                "INSERT INTO sources(path,name,duration_ms,size_bytes,modified_unix_ms) VALUES(?,?,?,?,?)",
                ("C:/capture.mp4", "capture.mp4", 12_000, 1_000_000, 1),
            ).lastrowid
            connection.execute(
                "INSERT INTO jobs(id,source_id,status,stage,analysis_version,config_json) VALUES(?,?,?,?,?,?)",
                ("job-1", source_id, "completed", "completed", "0.1.0", '{"pre_context_ms":15000}'),
            )
            connection.execute(
                "INSERT INTO candidates(job_id,start_ms,peak_ms,end_ms,score,signals_json,evidence_json,explanation) VALUES(?,?,?,?,?,?,?,?)",
                ("job-1", 0, 2_000, 12_000, 72, '["audio_anomaly"]', '[]', "Audio anomaly"),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = sqlite3.connect(database_path)
        try:
            result = reopened.execute(
                "SELECT j.status,c.peak_ms,c.score FROM jobs j JOIN candidates c ON c.job_id=j.id WHERE j.id='job-1'"
            ).fetchone()
            self.assertEqual(result, ("completed", 2_000, 72))
        finally:
            reopened.close()
            database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

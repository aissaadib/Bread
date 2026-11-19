use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use serde_json::Value;
use std::{fs, path::Path};

use crate::MediaInfo;

const INITIAL_SCHEMA: &str = include_str!("../migrations/001_initial.sql");
const CLIP_EXPORT_SCHEMA: &str = include_str!("../migrations/002_clip_exports.sql");

pub(crate) struct Database {
    connection: Connection,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryEntry {
    pub id: String,
    pub source_name: String,
    pub path: String,
    pub status: String,
    pub stage: String,
    pub progress: u8,
    pub created_at: String,
    pub error_message: Option<String>,
    pub notices: Vec<String>,
    pub candidates: Vec<HistoryCandidate>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryCandidate {
    pub start_ms: u64,
    pub peak_ms: u64,
    pub end_ms: u64,
    pub score: u8,
    pub signals: Vec<String>,
    pub explanation: String,
    pub title: String,
    pub title_source: String,
    pub clip_path: Option<String>,
}

impl Database {
    pub(crate) fn open(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("Cannot create app data folder: {e}"))?;
        }
        let connection = Connection::open(path).map_err(|e| format!("Cannot open local database: {e}"))?;
        connection.busy_timeout(std::time::Duration::from_secs(5)).map_err(db_error)?;
        connection.pragma_update(None, "foreign_keys", "ON").map_err(db_error)?;
        connection.pragma_update(None, "journal_mode", "WAL").map_err(db_error)?;
        let mut version: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0)).map_err(db_error)?;
        if version == 0 {
            connection.execute_batch(INITIAL_SCHEMA).map_err(db_error)?;
            version = 1;
        }
        if version == 1 {
            connection.execute_batch(CLIP_EXPORT_SCHEMA).map_err(db_error)?;
            version = 2;
        }
        if version > 2 {
            return Err(format!("Database version {version} is newer than this Luna build."));
        }
        connection.execute(
            "UPDATE jobs SET status='failed', stage='interrupted', error_code='app_closed', error_message='The previous analysis stopped before it finished.', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status IN ('queued','running','cancelling')",
            [],
        ).map_err(db_error)?;
        Ok(Self { connection })
    }

    pub(crate) fn register_source(&mut self, media: &MediaInfo) -> Result<i64, String> {
        let modified_unix_ms = fs::metadata(&media.path).ok()
            .and_then(|meta| meta.modified().ok())
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|duration| duration.as_millis().min(i64::MAX as u128) as i64)
            .unwrap_or_default();
        self.connection.execute(
            "INSERT INTO sources(path,name,duration_ms,size_bytes,width,height,frame_rate,video_codec,audio_codec,audio_tracks,modified_unix_ms) \
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11) \
             ON CONFLICT(path) DO UPDATE SET name=excluded.name,duration_ms=excluded.duration_ms,size_bytes=excluded.size_bytes,\
             width=excluded.width,height=excluded.height,frame_rate=excluded.frame_rate,video_codec=excluded.video_codec,\
             audio_codec=excluded.audio_codec,audio_tracks=excluded.audio_tracks,modified_unix_ms=excluded.modified_unix_ms",
            params![media.path, media.name, (media.duration_seconds * 1000.0).round() as i64,
                media.size_bytes.min(i64::MAX as u64) as i64, media.width.map(|v| v as i64), media.height.map(|v| v as i64),
                media.frame_rate, media.video_codec, media.audio_codec,
                media.audio_tracks.min(i64::MAX as u64) as i64, modified_unix_ms],
        ).map_err(db_error)?;
        self.connection.query_row("SELECT id FROM sources WHERE path=?1", [&media.path], |row| row.get(0)).map_err(db_error)
    }

    pub(crate) fn source_id(&self, path: &str) -> Result<Option<i64>, String> {
        self.connection.query_row("SELECT id FROM sources WHERE path=?1", [path], |row| row.get(0))
            .optional().map_err(db_error)
    }

    pub(crate) fn source_duration_seconds(&self, source_id: i64) -> Result<f64, String> {
        self.connection.query_row(
            "SELECT duration_ms FROM sources WHERE id=?1",
            [source_id],
            |row| row.get::<_, i64>(0),
        ).map(|duration_ms| duration_ms.max(0) as f64 / 1000.0).map_err(db_error)
    }

    pub(crate) fn create_job(&mut self, id: &str, source_id: i64, config_json: &str) -> Result<(), String> {
        let analysis_version = serde_json::from_str::<serde_json::Value>(config_json)
            .ok()
            .and_then(|config| config.get("analysis_version").and_then(|value| value.as_str()).map(str::to_owned))
            .unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_string());
        self.connection.execute(
            "INSERT INTO jobs(id,source_id,status,stage,progress,analysis_version,config_json) VALUES(?1,?2,'queued','queued',0,?4,?3)",
            params![id, source_id, config_json, analysis_version],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn set_job_failed(&mut self, id: &str, code: &str, message: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE jobs SET status='failed',stage='failed',error_code=?2,error_message=?3,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
            params![id, code, message],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn mark_cancelling(&mut self, id: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE jobs SET status='cancelling',stage='cancelling',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1 AND status IN ('queued','running')",
            [id],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn update_event(&mut self, event: &Value) -> Result<(), String> {
        let id = event.get("job_id").and_then(Value::as_str).ok_or("Worker event has no job_id")?;
        let kind = event.get("type").and_then(Value::as_str).ok_or("Worker event has no type")?;
        let progress = event.get("progress").and_then(Value::as_u64).unwrap_or(0).min(100) as i64;
        let stage = event.get("stage").and_then(Value::as_str).unwrap_or(kind);
        match kind {
            "candidate" => {
                let candidate = event.get("candidate").ok_or("Worker candidate payload is missing")?;
                let signals = candidate.get("signals").cloned().unwrap_or(Value::Array(Vec::new()));
                let evidence = candidate.get("evidence").cloned().unwrap_or(Value::Array(Vec::new()));
                self.connection.execute(
                    "INSERT OR REPLACE INTO candidates(job_id,start_ms,peak_ms,end_ms,score,signals_json,evidence_json,explanation,title,title_source,clip_path) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
                    params![id,
                        candidate.get("start_ms").and_then(Value::as_i64).unwrap_or(0),
                        candidate.get("peak_ms").and_then(Value::as_i64).unwrap_or(0),
                        candidate.get("end_ms").and_then(Value::as_i64).unwrap_or(0),
                        candidate.get("score").and_then(Value::as_i64).unwrap_or(0),
                        signals.to_string(), evidence.to_string(),
                        candidate.get("explanation").and_then(Value::as_str).unwrap_or("Detected event"),
                        candidate.get("title").and_then(Value::as_str).unwrap_or("Highlight"),
                        candidate.get("title_source").and_then(Value::as_str).unwrap_or("evidence_fallback"),
                        candidate.get("clip_path").and_then(Value::as_str)],
                ).map_err(db_error)?;
            }
            "stage" | "progress" => {
                self.connection.execute(
                    "UPDATE jobs SET status='running',stage=?2,progress=MAX(progress,?3),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
                    params![id, stage, progress],
                ).map_err(db_error)?;
            }
            "complete" => {
                let notices = event.get("notices").cloned().unwrap_or(Value::Array(Vec::new())).to_string();
                self.connection.execute(
                    "UPDATE jobs SET status='completed',stage='completed',progress=100,notices_json=?2,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
                    params![id, notices],
                ).map_err(db_error)?;
            }
            "cancelled" => {
                self.connection.execute(
                    "UPDATE jobs SET status='cancelled',stage='cancelled',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
                    [id],
                ).map_err(db_error)?;
            }
            "error" => {
                let code = event.get("code").and_then(Value::as_str).unwrap_or("worker_error");
                let message = event.get("message").and_then(Value::as_str).unwrap_or("The analysis worker failed.");
                self.set_job_failed(id, code, message)?;
            }
            "notice" => {}
            _ => return Err(format!("Unknown worker event type: {kind}")),
        }
        Ok(())
    }

    pub(crate) fn history(&self) -> Result<Vec<HistoryEntry>, String> {
        let mut statement = self.connection.prepare(
            "SELECT j.id,s.name,s.path,j.status,j.stage,j.progress,j.created_at,j.error_message,j.notices_json \
             FROM jobs j JOIN sources s ON s.id=j.source_id ORDER BY j.created_at DESC LIMIT 50",
        ).map_err(db_error)?;
        let entries = statement.query_map([], |row| {
            let notices_json: String = row.get(8)?;
            let notices = serde_json::from_str(&notices_json).unwrap_or_default();
            Ok(HistoryEntry {
                id: row.get(0)?, source_name: row.get(1)?, path: row.get(2)?, status: row.get(3)?,
                stage: row.get(4)?, progress: row.get::<_, i64>(5)?.clamp(0, 100) as u8,
                created_at: row.get(6)?, error_message: row.get(7)?, notices, candidates: Vec::new(),
            })
        }).map_err(db_error)?;
        let mut history = Vec::new();
        for entry in entries {
            let mut entry = entry.map_err(db_error)?;
            entry.candidates = self.candidates(&entry.id)?;
            history.push(entry);
        }
        Ok(history)
    }

    fn candidates(&self, job_id: &str) -> Result<Vec<HistoryCandidate>, String> {
        let mut statement = self.connection.prepare(
            "SELECT start_ms,peak_ms,end_ms,score,signals_json,explanation,title,title_source,clip_path FROM candidates WHERE job_id=?1 ORDER BY start_ms",
        ).map_err(db_error)?;
        let rows = statement.query_map([job_id], |row| {
            let raw_signals: String = row.get(4)?;
            let signals = serde_json::from_str(&raw_signals).unwrap_or_default();
            Ok(HistoryCandidate {
                start_ms: row.get::<_, i64>(0)?.max(0) as u64,
                peak_ms: row.get::<_, i64>(1)?.max(0) as u64,
                end_ms: row.get::<_, i64>(2)?.max(0) as u64,
                score: row.get::<_, i64>(3)?.clamp(0, 100) as u8,
                signals, explanation: row.get(5)?, title: row.get(6)?,
                title_source: row.get(7)?, clip_path: row.get(8)?,
            })
        }).map_err(db_error)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
    }
}

fn db_error(error: rusqlite::Error) -> String {
    format!("Local database error: {error}")
}

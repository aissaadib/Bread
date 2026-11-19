use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use serde_json::Value;
use std::{fs, path::Path};

use crate::MediaInfo;

const INITIAL_SCHEMA: &str = include_str!("../migrations/001_initial.sql");
const CLIP_EXPORT_SCHEMA: &str = include_str!("../migrations/002_clip_exports.sql");
const SHORTFORM_EXPORT_SCHEMA: &str = include_str!("../migrations/003_shortform_exports.sql");
const AI_ANALYSIS_SCHEMA: &str = include_str!("../migrations/004_ai_analysis.sql");

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
    pub ai_status: String,
    pub ai_error: Option<String>,
    pub ai_model: Option<String>,
    pub ai_windows: u32,
    pub ai_message: Option<String>,
    pub candidates: Vec<HistoryCandidate>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HistoryCandidate {
    pub id: i64,
    pub start_ms: u64,
    pub peak_ms: u64,
    pub end_ms: u64,
    pub score: u8,
    pub signals: Vec<String>,
    pub explanation: String,
    pub title: String,
    pub title_source: String,
    pub clip_path: Option<String>,
    pub origin: String,
    pub ai_title: Option<String>,
    pub ai_event_type: Option<String>,
    pub ai_reason: Option<String>,
    pub ai_score: Option<u8>,
    pub ai_peak_ms: Option<u64>,
    pub ai_focus_x: Option<f64>,
    pub ai_focus_y: Option<f64>,
    pub combined_score: Option<u8>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExportRecord {
    pub id: String,
    pub batch_id: String,
    pub job_id: String,
    pub candidate_id: Option<i64>,
    pub source_path: String,
    pub title: String,
    pub start_ms: u64,
    pub end_ms: u64,
    pub peak_ms: u64,
    pub aspect: String,
    pub mode: String,
    pub crop_x: f64,
    pub crop_y: f64,
    pub captions: bool,
    pub crf: u8,
    pub output_path: Option<String>,
    pub status: String,
    pub stage: String,
    pub progress: u8,
    pub error_message: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Clone)]
pub struct NewExportRow {
    pub id: String,
    pub candidate_id: Option<i64>,
    pub title: String,
    pub start_ms: u64,
    pub end_ms: u64,
    pub peak_ms: u64,
}

pub struct JobSource {
    pub path: String,
    pub name: String,
    pub duration_ms: i64,
    pub transcript: Vec<Value>,
    pub status: String,
}

pub struct CandidateRecord {
    pub id: i64,
    pub start_ms: u64,
    pub peak_ms: u64,
    pub end_ms: u64,
    pub title: String,
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
        if version == 2 {
            connection.execute_batch(SHORTFORM_EXPORT_SCHEMA).map_err(db_error)?;
            version = 3;
        }
        if version == 3 {
            connection.execute_batch(AI_ANALYSIS_SCHEMA).map_err(db_error)?;
            version = 4;
        }
        if version > 4 {
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
                let transcript = event.get("transcript").cloned().unwrap_or(Value::Array(Vec::new())).to_string();
                self.connection.execute(
                    "UPDATE jobs SET status='completed',stage='completed',progress=100,notices_json=?2,transcript_json=?3,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
                    params![id, notices, transcript],
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
            "SELECT j.id,s.name,s.path,j.status,j.stage,j.progress,j.created_at,j.error_message,j.notices_json,\
             j.ai_status,j.ai_error,j.ai_model,j.ai_windows,j.ai_message \
             FROM jobs j JOIN sources s ON s.id=j.source_id ORDER BY j.created_at DESC LIMIT 50",
        ).map_err(db_error)?;
        let entries = statement.query_map([], |row| {
            let notices_json: String = row.get(8)?;
            let notices = serde_json::from_str(&notices_json).unwrap_or_default();
            Ok(HistoryEntry {
                id: row.get(0)?, source_name: row.get(1)?, path: row.get(2)?, status: row.get(3)?,
                stage: row.get(4)?, progress: row.get::<_, i64>(5)?.clamp(0, 100) as u8,
                created_at: row.get(6)?, error_message: row.get(7)?, notices,
                ai_status: row.get(9)?, ai_error: row.get(10)?, ai_model: row.get(11)?,
                ai_windows: row.get::<_, i64>(12)?.max(0) as u32, ai_message: row.get(13)?,
                candidates: Vec::new(),
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

    pub(crate) fn candidates(&self, job_id: &str) -> Result<Vec<HistoryCandidate>, String> {
        let mut statement = self.connection.prepare(
            "SELECT id,start_ms,peak_ms,end_ms,score,signals_json,explanation,title,title_source,clip_path,\
             origin,ai_title,ai_event_type,ai_reason,ai_score,ai_peak_ms,ai_focus_x,ai_focus_y,combined_score \
             FROM candidates WHERE job_id=?1 ORDER BY start_ms",
        ).map_err(db_error)?;
        let rows = statement.query_map([job_id], |row| {
            let raw_signals: String = row.get(5)?;
            let signals = serde_json::from_str(&raw_signals).unwrap_or_default();
            Ok(HistoryCandidate {
                id: row.get(0)?,
                start_ms: row.get::<_, i64>(1)?.max(0) as u64,
                peak_ms: row.get::<_, i64>(2)?.max(0) as u64,
                end_ms: row.get::<_, i64>(3)?.max(0) as u64,
                score: row.get::<_, i64>(4)?.clamp(0, 100) as u8,
                signals, explanation: row.get(6)?, title: row.get(7)?,
                title_source: row.get(8)?, clip_path: row.get(9)?,
                origin: row.get(10)?,
                ai_title: row.get(11)?,
                ai_event_type: row.get(12)?,
                ai_reason: row.get(13)?,
                ai_score: row.get::<_, Option<i64>>(14)?.map(|value| value.clamp(0, 100) as u8),
                ai_peak_ms: row.get::<_, Option<i64>>(15)?.map(|value| value.max(0) as u64),
                ai_focus_x: row.get(16)?,
                ai_focus_y: row.get(17)?,
                combined_score: row.get::<_, Option<i64>>(18)?.map(|value| value.clamp(0, 100) as u8),
            })
        }).map_err(db_error)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
    }

    pub(crate) fn setting(&self, key: &str) -> Result<Option<String>, String> {
        self.connection.query_row(
            "SELECT value FROM app_settings WHERE key=?1", [key], |row| row.get(0),
        ).optional().map_err(db_error)
    }

    pub(crate) fn set_setting(&mut self, key: &str, value: &str) -> Result<(), String> {
        self.connection.execute(
            "INSERT INTO app_settings(key,value) VALUES(?1,?2)\
             ON CONFLICT(key) DO UPDATE SET value=excluded.value,\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            params![key, value],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn set_job_ai_status(&mut self, job_id: &str, status: &str,
                                    message: Option<&str>, model: Option<&str>,
                                    windows: Option<i64>) -> Result<(), String> {
        self.connection.execute(
            "UPDATE jobs SET ai_status=?2, ai_message=COALESCE(?3, ai_message),\
             ai_model=COALESCE(?4, ai_model), ai_windows=COALESCE(?5, ai_windows),\
             ai_error=CASE WHEN ?2 IN ('failed','rate_limited') THEN ?3 ELSE NULL END,\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
            params![job_id, status, message, model, windows],
        ).map_err(db_error)?;
        Ok(())
    }

    /// Merge one validated cloud assessment with the existing local evidence.
    /// Local columns are never overwritten; AI evidence gets its own columns.
    pub(crate) fn apply_ai_item(&mut self, job_id: &str, item: &Value) -> Result<(), String> {
        if item.get("rejected").and_then(Value::as_bool).unwrap_or(false) {
            return Ok(());
        }
        let is_highlight = item.get("isHighlight").and_then(Value::as_bool).unwrap_or(false);
        let window_start = item.get("windowStartMs").and_then(Value::as_i64).unwrap_or(0).max(0);
        let window_end = item.get("windowEndMs").and_then(Value::as_i64).unwrap_or(0).max(window_start + 1);
        let interest = item.get("interestScore").and_then(Value::as_i64).unwrap_or(0).clamp(0, 100);
        let combined = item.get("combinedScore").and_then(Value::as_i64).unwrap_or(0).clamp(0, 100);
        let event_type = item.get("eventType").and_then(Value::as_str).unwrap_or("other");
        let reason = item.get("reason").and_then(Value::as_str).unwrap_or("");
        let model = item.get("model").and_then(Value::as_str).unwrap_or("gemini");
        let ai_title = item.get("aiTitle").and_then(Value::as_str);
        let focus_x = item.get("focusX").and_then(Value::as_f64);
        let focus_y = item.get("focusY").and_then(Value::as_f64);
        let focus_confidence = item.get("focusConfidence").and_then(Value::as_f64).unwrap_or(0.0);
        let mostly_menu = item.get("mostlyMenu").and_then(Value::as_bool);
        let visually_clear = item.get("visuallyClear").and_then(Value::as_bool);
        let ai_peak = item.get("peakMs").and_then(Value::as_i64).map(|value| value.clamp(window_start, window_end));
        let candidate_id = item.get("candidateId").and_then(Value::as_i64);

        let target = match candidate_id {
            Some(id) => self.connection.query_row(
                "SELECT id FROM candidates WHERE id=?1 AND job_id=?2", params![id, job_id],
                |row| row.get::<_, i64>(0),
            ).optional().map_err(db_error)?,
            None => None,
        };
        let target = match (target, ai_peak) {
            (Some(id), _) => Some(id),
            (None, Some(peak)) => self.connection.query_row(
                "SELECT id FROM candidates WHERE job_id=?1 AND peak_ms BETWEEN ?2 AND ?3 ORDER BY ABS(peak_ms-?4) LIMIT 1",
                params![job_id, peak - 2000, peak + 2000, peak],
                |row| row.get::<_, i64>(0),
            ).optional().map_err(db_error)?,
            (None, None) => None,
        };

        if let Some(id) = target {
            self.connection.execute(
                "UPDATE candidates SET ai_title=?2, ai_event_type=?3, ai_reason=?4, ai_score=?5,\
                 ai_peak_ms=?6, ai_focus_x=?7, ai_focus_y=?8, combined_score=?9 WHERE id=?1",
                params![id, ai_title, event_type, reason, interest, ai_peak,
                        focus_x, focus_y, combined],
            ).map_err(db_error)?;
        } else if is_highlight {
            if let Some(peak) = ai_peak {
                let start = window_start.min(peak);
                let end = window_end.max(peak);
                let signals = serde_json::json!(["ai_semantic"]).to_string();
                let evidence = serde_json::json!([]).to_string();
                let title = ai_title.filter(|value| !value.trim().is_empty()).unwrap_or("AI highlight");
                let explanation = if reason.trim().is_empty() {
                    format!("Cloud AI found a {event_type} moment")
                } else {
                    format!("Cloud AI: {reason}")
                };
                self.connection.execute(
                    "INSERT INTO candidates(job_id,start_ms,peak_ms,end_ms,score,signals_json,\
                     evidence_json,explanation,title,title_source,clip_path,origin,ai_title,\
                     ai_event_type,ai_reason,ai_score,ai_peak_ms,ai_focus_x,ai_focus_y,combined_score)\
                     VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,'gemini_cloud',NULL,'gemini',?9,?10,?11,?12,?13,?14,?15,?16)\
                     ON CONFLICT(job_id,peak_ms) DO UPDATE SET ai_title=excluded.ai_title,\
                     ai_event_type=excluded.ai_event_type, ai_reason=excluded.ai_reason,\
                     ai_score=excluded.ai_score, ai_peak_ms=excluded.ai_peak_ms,\
                     ai_focus_x=excluded.ai_focus_x, ai_focus_y=excluded.ai_focus_y,\
                     combined_score=excluded.combined_score",
                    params![job_id, start, peak, end, combined, signals, evidence, explanation,
                            title, event_type, reason, interest, ai_peak, focus_x, focus_y, combined],
                ).map_err(db_error)?;
            }
        }

        self.connection.execute(
            "INSERT INTO ai_assessments(job_id,candidate_id,window_start_ms,window_end_ms,\
             is_highlight,event_type,interest_score,peak_ms,reason,mostly_menu,visually_clear,\
             focus_x,focus_y,focus_confidence,combined_score,origin,model)\
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17)",
            params![job_id, target.or(candidate_id), window_start, window_end,
                    if is_highlight { 1 } else { 0 }, event_type, interest, ai_peak, reason,
                    mostly_menu.map(|value| if value { 1 } else { 0 }),
                    visually_clear.map(|value| if value { 1 } else { 0 }),
                    focus_x, focus_y, focus_confidence, combined,
                    item.get("origin").and_then(Value::as_str).unwrap_or("candidate"), model],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn job_source(&self, job_id: &str) -> Result<Option<JobSource>, String> {
        self.connection.query_row(
            "SELECT s.path,s.name,s.duration_ms,j.transcript_json,j.status
             FROM jobs j JOIN sources s ON s.id=j.source_id WHERE j.id=?1",
            [job_id],
            |row| {
                let transcript_json: String = row.get(3)?;
                let transcript = serde_json::from_str(&transcript_json).unwrap_or_default();
                Ok(JobSource {
                    path: row.get(0)?, name: row.get(1)?,
                    duration_ms: row.get(2)?, transcript, status: row.get(4)?,
                })
            },
        ).optional().map_err(db_error)
    }

    pub(crate) fn transcript(&self, job_id: &str) -> Result<Vec<Value>, String> {
        let raw: Option<String> = self.connection.query_row(
            "SELECT transcript_json FROM jobs WHERE id=?1", [job_id], |row| row.get(0),
        ).optional().map_err(db_error)?;
        Ok(raw.and_then(|value| serde_json::from_str(&value).ok()).unwrap_or_default())
    }

    pub(crate) fn candidate_for_export(&self, job_id: &str, candidate_id: Option<i64>,
                                       peak_ms: u64) -> Result<Option<CandidateRecord>, String> {
        if let Some(id) = candidate_id {
            let found = self.connection.query_row(
                "SELECT id,start_ms,peak_ms,end_ms,title FROM candidates WHERE id=?1 AND job_id=?2",
                params![id, job_id],
                |row| Ok(CandidateRecord {
                    id: row.get(0)?,
                    start_ms: row.get::<_, i64>(1)?.max(0) as u64,
                    peak_ms: row.get::<_, i64>(2)?.max(0) as u64,
                    end_ms: row.get::<_, i64>(3)?.max(0) as u64,
                    title: row.get(4)?,
                }),
            ).optional().map_err(db_error)?;
            if found.is_some() {
                return Ok(found);
            }
        }
        self.connection.query_row(
            "SELECT id,start_ms,peak_ms,end_ms,title FROM candidates WHERE job_id=?1 AND peak_ms=?2 ORDER BY id LIMIT 1",
            params![job_id, peak_ms as i64],
            |row| Ok(CandidateRecord {
                id: row.get(0)?,
                start_ms: row.get::<_, i64>(1)?.max(0) as u64,
                peak_ms: row.get::<_, i64>(2)?.max(0) as u64,
                end_ms: row.get::<_, i64>(3)?.max(0) as u64,
                title: row.get(4)?,
            }),
        ).optional().map_err(db_error)
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn create_exports(&mut self, batch_id: &str, job_id: &str, source_path: &str,
                                 aspect: &str, mode: &str, crop_x: f64, crop_y: f64,
                                 captions: bool, crf: u8, rows: &[NewExportRow]) -> Result<(), String> {
        let transaction = self.connection.transaction().map_err(db_error)?;
        for row in rows {
            transaction.execute(
                "INSERT INTO clip_exports(id,batch_id,job_id,candidate_id,source_path,title,start_ms,end_ms,peak_ms,\
                 aspect,mode,crop_x,crop_y,captions,crf,status,stage) \
                 VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,'queued','waiting')",
                params![row.id, batch_id, job_id, row.candidate_id, source_path, row.title,
                        row.start_ms as i64, row.end_ms as i64, row.peak_ms as i64,
                        aspect, mode, crop_x, crop_y, if captions { 1 } else { 0 }, crf as i64],
            ).map_err(db_error)?;
        }
        transaction.commit().map_err(db_error)
    }

    pub(crate) fn exports_for_batch(&self, batch_id: &str) -> Result<Vec<ExportRecord>, String> {
        let mut statement = self.connection.prepare(
            "SELECT id,batch_id,job_id,candidate_id,source_path,title,start_ms,end_ms,peak_ms,\
             aspect,mode,crop_x,crop_y,captions,crf,output_path,status,stage,progress,error_message,\
             created_at,updated_at FROM clip_exports WHERE batch_id=?1 ORDER BY created_at,rowid",
        ).map_err(db_error)?;
        let rows = statement.query_map([batch_id], export_from_row).map_err(db_error)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
    }

    pub(crate) fn list_exports(&self, job_id: Option<&str>, limit: u32) -> Result<Vec<ExportRecord>, String> {
        let mut statement = self.connection.prepare(
            "SELECT id,batch_id,job_id,candidate_id,source_path,title,start_ms,end_ms,peak_ms,\
             aspect,mode,crop_x,crop_y,captions,crf,output_path,status,stage,progress,error_message,\
             created_at,updated_at FROM clip_exports \
             WHERE (?1 IS NULL OR job_id=?1) ORDER BY created_at DESC,rowid DESC LIMIT ?2",
        ).map_err(db_error)?;
        let rows = statement.query_map(params![job_id, limit.clamp(1, 200) as i64], export_from_row)
            .map_err(db_error)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
    }

    pub(crate) fn export_record(&self, id: &str) -> Result<Option<ExportRecord>, String> {
        self.connection.query_row(
            "SELECT id,batch_id,job_id,candidate_id,source_path,title,start_ms,end_ms,peak_ms,\
             aspect,mode,crop_x,crop_y,captions,crf,output_path,status,stage,progress,error_message,\
             created_at,updated_at FROM clip_exports WHERE id=?1",
            [id], export_from_row,
        ).optional().map_err(db_error)
    }

    pub(crate) fn update_export_started(&mut self, id: &str, output_path: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET output_path=?2,status='preparing',stage='preparing',progress=0,\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
            params![id, output_path],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn update_export_progress(&mut self, id: &str, progress: u8) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET status='rendering',stage='rendering',progress=?2,\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1 AND status NOT IN ('completed','failed','cancelled')",
            params![id, progress.clamp(0, 100) as i64],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn update_export_stage(&mut self, id: &str, stage: &str) -> Result<(), String> {
        let status = match stage {
            "finalizing" => "finalizing",
            "preparing" => "preparing",
            _ => "rendering",
        };
        self.connection.execute(
            "UPDATE clip_exports SET status=?2,stage=?3,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') \
             WHERE id=?1 AND status NOT IN ('completed','failed','cancelled')",
            params![id, status, stage],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn complete_export(&mut self, id: &str, output_path: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET output_path=?2,status='completed',stage='completed',progress=100,\
             error_message=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
            params![id, output_path],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn fail_export(&mut self, id: &str, message: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET status='failed',stage='failed',error_message=?2,\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
            params![id, message],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn cancel_export_row(&mut self, id: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET status='cancelled',stage='cancelled',\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1 \
             AND status NOT IN ('completed','failed')",
            [id],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn cancel_remaining_exports(&mut self, batch_id: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET status='cancelled',stage='cancelled',\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE batch_id=?1 \
             AND status NOT IN ('completed','failed','cancelled')",
            [batch_id],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn fail_remaining_exports(&mut self, batch_id: &str, message: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET status='failed',stage='failed',error_message=?2,\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE batch_id=?1 \
             AND status NOT IN ('completed','failed','cancelled')",
            params![batch_id, message],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn mark_batch_cancelling(&mut self, batch_id: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET status='cancelling',stage='cancelling',\
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE batch_id=?1 \
             AND status NOT IN ('completed','failed','cancelled')",
            [batch_id],
        ).map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn reset_export_for_retry(&mut self, id: &str, batch_id: &str) -> Result<(), String> {
        self.connection.execute(
            "UPDATE clip_exports SET batch_id=?2,status='queued',stage='waiting',progress=0,\
             output_path=NULL,error_message=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?1",
            params![id, batch_id],
        ).map_err(db_error)?;
        Ok(())
    }
}

fn export_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ExportRecord> {
    Ok(ExportRecord {
        id: row.get(0)?,
        batch_id: row.get(1)?,
        job_id: row.get(2)?,
        candidate_id: row.get(3)?,
        source_path: row.get(4)?,
        title: row.get(5)?,
        start_ms: row.get::<_, i64>(6)?.max(0) as u64,
        end_ms: row.get::<_, i64>(7)?.max(0) as u64,
        peak_ms: row.get::<_, i64>(8)?.max(0) as u64,
        aspect: row.get(9)?,
        mode: row.get(10)?,
        crop_x: row.get(11)?,
        crop_y: row.get(12)?,
        captions: row.get::<_, i64>(13)? != 0,
        crf: row.get::<_, i64>(14)?.clamp(0, 100) as u8,
        output_path: row.get(15)?,
        status: row.get(16)?,
        stage: row.get(17)?,
        progress: row.get::<_, i64>(18)?.clamp(0, 100) as u8,
        error_message: row.get(19)?,
        created_at: row.get(20)?,
        updated_at: row.get(21)?,
    })
}

fn db_error(error: rusqlite::Error) -> String {
    format!("Local database error: {error}")
}

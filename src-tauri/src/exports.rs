use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter};

use crate::{logic, resolve_export_worker, resolve_python, storage::NewExportRow, AppState};

const MAX_BATCH_ITEMS: usize = 50;
const EXPORT_IDLE_TIMEOUT: Duration = Duration::from_secs(15 * 60);
const EXPORT_MAX_RUNTIME: Duration = Duration::from_secs(2 * 60 * 60);
const MAX_MESSAGE_BYTES: usize = 1_048_576;

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExportItemRequest {
    pub candidate_id: Option<i64>,
    pub peak_ms: u64,
    pub start_ms: u64,
    pub end_ms: u64,
    pub title: String,
}

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExportRequest {
    pub job_id: String,
    pub aspect: String,
    pub mode: String,
    pub crop_x: f64,
    pub crop_y: f64,
    pub captions: bool,
    pub crf: u8,
    pub output_dir: String,
    pub items: Vec<ExportItemRequest>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StartExportResult {
    pub batch_id: String,
    pub exports: Vec<crate::storage::ExportRecord>,
}

pub struct ActiveExport {
    batch_id: String,
    stdin: ChildStdin,
    pid: u32,
    cancelling: Arc<AtomicBool>,
}

pub fn start(app: &AppHandle, state: &Arc<AppState>, request: ExportRequest) -> Result<StartExportResult, String> {
    if request.items.is_empty() || request.items.len() > MAX_BATCH_ITEMS {
        return Err(format!("Choose between 1 and {MAX_BATCH_ITEMS} clips to export."));
    }
    let (aspect, mode) = logic::validate_export_settings(
        &request.aspect, &request.mode, request.crop_x, request.crop_y, request.crf,
    )?;
    {
        let slot = state.export.lock().map_err(|_| "Export lock is unavailable.".to_string())?;
        if slot.is_some() {
            return Err("Another short-form export is already running. Wait for it to finish or cancel it first.".into());
        }
    }
    let output_dir = PathBuf::from(request.output_dir.trim());
    if !output_dir.is_absolute() {
        return Err("Choose an absolute export folder.".into());
    }
    fs::create_dir_all(&output_dir)
        .map_err(|error| format!("Could not create the export folder: {error}"))?;

    let (job, rows) = {
        let database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        let job = database.job_source(&request.job_id)?
            .ok_or_else(|| "This analysis is no longer available. Run the analysis again before exporting.".to_string())?;
        if job.status != "completed" {
            return Err("Finish the analysis before exporting short-form clips.".into());
        }
        if !Path::new(&job.path).is_file() {
            return Err("The source recording is no longer available at its original path.".into());
        }
        if request.captions && job.transcript.is_empty() {
            return Err("Captions are not available for this recording because no speech transcript was saved. Export without captions or analyze a recording with local transcription enabled.".into());
        }
        let mut rows = Vec::with_capacity(request.items.len());
        for item in &request.items {
            let candidate = database.candidate_for_export(&request.job_id, item.candidate_id, item.peak_ms)?;
            let (candidate_id, peak_ms, title) = match &candidate {
                Some(candidate) => (Some(candidate.id), candidate.peak_ms, candidate.title.clone()),
                None => (None, item.peak_ms, item.title.clone()),
            };
            logic::validate_clip_range(item.start_ms, item.end_ms, job.duration_ms.max(0) as u64)?;
            rows.push(NewExportRow {
                id: logic::new_export_id(),
                candidate_id,
                title: if title.trim().is_empty() { "Highlight".to_string() } else { title },
                start_ms: item.start_ms,
                end_ms: item.end_ms,
                peak_ms,
            });
        }
        (job, rows)
    };

    let batch_id = logic::new_batch_id();
    {
        let mut database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        database.create_exports(
            &batch_id, &request.job_id, &job.path, &aspect, &mode,
            request.crop_x, request.crop_y, request.captions, request.crf, &rows,
        )?;
    }
    launch(
        app, state, &batch_id, &job.path, &output_dir, &aspect, &mode,
        request.crop_x, request.crop_y, request.captions, request.crf,
        &job.transcript, &rows,
    )?;
    let exports = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .exports_for_batch(&batch_id)?;
    Ok(StartExportResult { batch_id, exports })
}

pub fn retry(app: &AppHandle, state: &Arc<AppState>, export_id: &str) -> Result<StartExportResult, String> {
    {
        let slot = state.export.lock().map_err(|_| "Export lock is unavailable.".to_string())?;
        if slot.is_some() {
            return Err("Another short-form export is already running. Wait for it to finish or cancel it first.".into());
        }
    }
    let record = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .export_record(export_id)?
        .ok_or_else(|| "This export record no longer exists.".to_string())?;
    if !matches!(record.status.as_str(), "failed" | "cancelled") {
        return Err("Only failed or cancelled exports can be retried.".into());
    }
    let batch_id = logic::new_batch_id();
    let (job, row) = {
        let mut database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        let job = database.job_source(&record.job_id)?
            .ok_or_else(|| "This analysis is no longer available.".to_string())?;
        if job.status != "completed" {
            return Err("Finish the analysis before exporting short-form clips.".into());
        }
        logic::validate_clip_range(record.start_ms, record.end_ms, job.duration_ms.max(0) as u64)?;
        if record.captions && job.transcript.is_empty() {
            return Err("Captions are no longer available for this recording.".into());
        }
        database.reset_export_for_retry(&record.id, &batch_id)?;
        let row = NewExportRow {
            id: record.id.clone(),
            candidate_id: record.candidate_id,
            title: record.title.clone(),
            start_ms: record.start_ms,
            end_ms: record.end_ms,
            peak_ms: record.peak_ms,
        };
        (job, row)
    };
    let output_dir = match record.output_path.as_deref().and_then(|path| Path::new(path).parent()) {
        Some(path) if path.is_dir() => path.to_path_buf(),
        _ => PathBuf::from(record.source_path.as_str()).parent()
            .map(|path| path.join("Luna Shorts"))
            .unwrap_or_else(|| PathBuf::from(".")),
    };
    launch(
        app, state, &batch_id, &job.path, &output_dir, &record.aspect, &record.mode,
        record.crop_x, record.crop_y, record.captions, record.crf,
        &job.transcript, &[row],
    )?;
    let exports = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .exports_for_batch(&batch_id)?;
    Ok(StartExportResult { batch_id, exports })
}

#[allow(clippy::too_many_arguments)]
fn launch(app: &AppHandle, state: &Arc<AppState>, batch_id: &str, source_path: &str,
          output_dir: &Path, aspect: &str, mode: &str, crop_x: f64, crop_y: f64,
          captions: bool, crf: u8, transcript: &[Value], rows: &[NewExportRow]) -> Result<(), String> {
    let request_dir = state.data_dir.join("export-requests");
    fs::create_dir_all(&request_dir)
        .map_err(|error| format!("Could not prepare the export request: {error}"))?;
    let request_path = request_dir.join(format!("{batch_id}.json"));
    let request = json!({
        "batchId": batch_id,
        "source": source_path,
        "outputDir": output_dir.to_string_lossy(),
        "aspect": aspect,
        "mode": mode,
        "cropX": crop_x,
        "cropY": crop_y,
        "crf": crf,
        "captions": captions,
        "transcript": transcript,
        "items": rows.iter().map(|row| json!({
            "id": row.id,
            "title": row.title,
            "startMs": row.start_ms,
            "endMs": row.end_ms,
        })).collect::<Vec<_>>(),
    });
    let encoded = serde_json::to_vec(&request)
        .map_err(|error| format!("Could not encode the export request: {error}"))?;
    fs::write(&request_path, encoded)
        .map_err(|error| format!("Could not write the export request: {error}"))?;

    let worker = resolve_export_worker(app)?;
    let (python, python_args) = resolve_python()?;
    let mut command = Command::new(python);
    command.args(python_args)
        .arg(worker)
        .arg("--request").arg(&request_path)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .env("PYTHONDONTWRITEBYTECODE", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            let _ = fs::remove_file(&request_path);
            let message = format!("Could not start the short-form renderer: {error}");
            if let Ok(mut database) = state.database.lock() {
                let _ = database.fail_remaining_exports(batch_id, &message);
            }
            return Err(message);
        }
    };
    let stdin = child.stdin.take().ok_or_else(|| "Could not open the export control channel.".to_string())?;
    let stdout = child.stdout.take().ok_or_else(|| "Could not read export progress.".to_string())?;
    let stderr = child.stderr.take().ok_or_else(|| "Could not read export diagnostics.".to_string())?;
    let cancelling = Arc::new(AtomicBool::new(false));
    let pid = child.id();
    {
        let mut slot = state.export.lock().map_err(|_| "Export lock is unavailable.".to_string())?;
        if slot.is_some() {
            let _ = child.kill();
            let _ = fs::remove_file(&request_path);
            return Err("Another short-form export started before this one could run.".into());
        }
        *slot = Some(ActiveExport {
            batch_id: batch_id.to_string(),
            stdin,
            pid,
            cancelling: Arc::clone(&cancelling),
        });
    }
    let app_thread = app.clone();
    let state_thread = Arc::clone(state);
    let thread_batch_id = batch_id.to_string();
    thread::spawn(move || {
        monitor_export(app_thread, state_thread, thread_batch_id, child, stdout, stderr,
                       cancelling, request_path);
    });
    Ok(())
}

pub fn cancel(state: &Arc<AppState>, batch_id: &str) -> Result<(), String> {
    let pid = {
        let mut slot = state.export.lock().map_err(|_| "Export lock is unavailable.".to_string())?;
        let active = slot.as_mut().ok_or_else(|| "This export is no longer running.".to_string())?;
        if active.batch_id != batch_id {
            return Err("This export is no longer running.".to_string());
        }
        active.cancelling.store(true, Ordering::SeqCst);
        serde_json::to_writer(&mut active.stdin, &json!({"command": "cancel", "batchId": batch_id}))
            .map_err(|error| format!("Could not cancel the export: {error}"))?;
        active.stdin.write_all(b"\n").and_then(|_| active.stdin.flush())
            .map_err(|error| format!("Could not cancel the export: {error}"))?;
        active.pid
    };
    state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .mark_batch_cancelling(batch_id)?;
    let state_clone = Arc::clone(state);
    let batch = batch_id.to_string();
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(8));
        let still_active = state_clone.export.lock().ok()
            .and_then(|slot| slot.as_ref().map(|active| active.batch_id == batch))
            .unwrap_or(false);
        if still_active {
            let _ = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
                .status();
        }
    });
    Ok(())
}

fn monitor_export(app: AppHandle, state: Arc<AppState>, batch_id: String, mut child: Child,
                  stdout: impl Read + Send + 'static, stderr: impl Read + Send + 'static,
                  cancelling: Arc<AtomicBool>, request_path: PathBuf) {
    let (sender, receiver) = mpsc::sync_channel(64);
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            let mut line = Vec::new();
            match reader.by_ref().take((MAX_MESSAGE_BYTES + 1) as u64).read_until(b'\n', &mut line) {
                Ok(0) => break,
                Ok(length) if length <= MAX_MESSAGE_BYTES && line.last() == Some(&b'\n') => {
                    if sender.send(Ok(line)).is_err() { break; }
                }
                Ok(_) => {
                    let _ = sender.send(Err("Export worker message exceeded the size limit.".to_string()));
                    break;
                }
                Err(error) => {
                    let _ = sender.send(Err(format!("Could not read export progress: {error}")));
                    break;
                }
            }
        }
    });
    let stderr_thread = thread::spawn(move || {
        let mut bytes = Vec::new();
        let mut reader = BufReader::new(stderr);
        let mut chunk = [0_u8; 4096];
        loop {
            match reader.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(length) if bytes.len() < 16_384 => {
                    let retained = length.min(16_384 - bytes.len());
                    bytes.extend_from_slice(&chunk[..retained]);
                }
                Ok(_) => {}
            }
        }
        String::from_utf8_lossy(&bytes).into_owned()
    });

    let mut terminal = false;
    let mut invalid_message = None;
    let deadline = Instant::now() + EXPORT_MAX_RUNTIME;
    let mut last_activity = Instant::now();
    loop {
        let remaining = EXPORT_IDLE_TIMEOUT.saturating_sub(last_activity.elapsed())
            .min(deadline.saturating_duration_since(Instant::now()));
        let output = match receiver.recv_timeout(remaining) {
            Ok(output) => output,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                invalid_message = Some(if last_activity.elapsed() >= EXPORT_IDLE_TIMEOUT {
                    "The export renderer stopped responding. The clip may be too complex for the current encoder settings.".to_string()
                } else {
                    "The export exceeded the two hour time limit.".to_string()
                });
                let _ = child.kill();
                break;
            }
        };
        match output {
            Ok(line) => {
                last_activity = Instant::now();
                let event: Value = match serde_json::from_slice(&line) {
                    Ok(event) => event,
                    Err(error) => {
                        invalid_message = Some(format!("Export worker sent invalid JSON: {error}"));
                        let _ = child.kill();
                        break;
                    }
                };
                if event.get("batchId").and_then(Value::as_str) != Some(batch_id.as_str()) {
                    invalid_message = Some("Export worker sent an event for an unexpected batch.".to_string());
                    let _ = child.kill();
                    break;
                }
                if let Ok(mut database) = state.database.lock() {
                    if let Err(error) = apply_export_event(&mut database, &event) {
                        invalid_message = Some(error);
                        let _ = child.kill();
                        break;
                    }
                }
                let kind = event.get("type").and_then(Value::as_str).unwrap_or_default();
                terminal |= matches!(kind, "batch_completed" | "error");
                let _ = app.emit("export-event", event);
            }
            Err(error) => {
                invalid_message = Some(error);
                let _ = child.kill();
                break;
            }
        }
    }

    drop(receiver);
    let _ = child.wait();
    let diagnostics = stderr_thread.join().unwrap_or_default();
    let was_cancelled = cancelling.load(Ordering::SeqCst);
    let failure = invalid_message.unwrap_or_else(|| {
        if diagnostics.trim().is_empty() {
            "The export renderer stopped before finishing.".to_string()
        } else {
            format!("The export renderer stopped: {}", diagnostics.trim())
        }
    });
    {
        if let Ok(mut database) = state.database.lock() {
            if terminal {
                if was_cancelled {
                    let _ = database.cancel_remaining_exports(&batch_id);
                } else {
                    let _ = database.fail_remaining_exports(
                        &batch_id, "The export finished before this clip was written.",
                    );
                }
            } else if was_cancelled {
                let _ = database.cancel_remaining_exports(&batch_id);
            } else {
                let _ = database.fail_remaining_exports(&batch_id, &failure);
            }
        }
    }
    if !terminal {
        let event = if was_cancelled {
            json!({"type":"batch_completed","batchId":batch_id,"cancelled":true,"completed":0,"failed":0})
        } else {
            json!({"type":"error","batchId":batch_id,"message":failure})
        };
        let _ = app.emit("export-event", event);
    }
    if let Ok(mut slot) = state.export.lock() {
        if slot.as_ref().is_some_and(|active| active.batch_id == batch_id) {
            *slot = None;
        }
    }
    let _ = fs::remove_file(&request_path);
}

fn apply_export_event(database: &mut crate::storage::Database, event: &Value) -> Result<(), String> {
    let kind = event.get("type").and_then(Value::as_str).unwrap_or_default();
    let item_id = event.get("itemId").and_then(Value::as_str).unwrap_or_default();
    match kind {
        "item_started" => {
            if let Some(path) = event.get("outputPath").and_then(Value::as_str) {
                database.update_export_started(item_id, path)?;
            }
        }
        "item_progress" => {
            let progress = event.get("progress").and_then(Value::as_f64).unwrap_or(0.0);
            database.update_export_progress(item_id, progress.round().clamp(0.0, 100.0) as u8)?;
        }
        "item_stage" => {
            let stage = event.get("stage").and_then(Value::as_str).unwrap_or("rendering");
            database.update_export_stage(item_id, stage)?;
        }
        "item_completed" => {
            if let Some(path) = event.get("outputPath").and_then(Value::as_str) {
                database.complete_export(item_id, path)?;
            }
        }
        "item_failed" => {
            let message = event.get("message").and_then(Value::as_str).unwrap_or("The clip could not be rendered.");
            database.fail_export(item_id, message)?;
        }
        "item_cancelled" => {
            database.cancel_export_row(item_id)?;
        }
        "error" => {
            let batch_id = event.get("batchId").and_then(Value::as_str).unwrap_or_default();
            let message = event.get("message").and_then(Value::as_str).unwrap_or("The export failed.");
            database.fail_remaining_exports(batch_id, message)?;
        }
        "batch_started" | "batch_completed" => {}
        _ => {}
    }
    Ok(())
}

pub fn open_export(state: &Arc<AppState>, export_id: &str, reveal: bool) -> Result<(), String> {
    let record = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .export_record(export_id)?
        .ok_or_else(|| "This export record no longer exists.".to_string())?;
    let path = record.output_path.ok_or_else(|| "This export has not produced a file yet.".to_string())?;
    if !Path::new(&path).is_file() {
        return Err("The exported file is no longer at its saved location.".to_string());
    }
    spawn_open(&path, reveal)
}

#[cfg(windows)]
fn spawn_open(path: &str, reveal: bool) -> Result<(), String> {
    let mut command = if reveal {
        let mut command = Command::new("explorer");
        command.arg(format!("/select,{path}"));
        command
    } else {
        let mut command = Command::new("cmd");
        command.args(["/C", "start", "", path]);
        command
    };
    command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
        .spawn().map(|_| ()).map_err(|error| format!("Could not open the exported file: {error}"))
}

#[cfg(not(windows))]
fn spawn_open(path: &str, reveal: bool) -> Result<(), String> {
    let target = if reveal {
        Path::new(path).parent().map(|parent| parent.to_string_lossy().to_string()).unwrap_or_else(|| path.to_string())
    } else {
        path.to_string()
    };
    let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
    Command::new(opener).arg(target).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
        .spawn().map(|_| ()).map_err(|error| format!("Could not open the exported file: {error}"))
}

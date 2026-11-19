//! Optional cloud semantic analysis (Google Gemini) hosted behind the native
//! layer. The key comes from the Windows Credential Manager, travels to the
//! Python worker over its stdin pipe, and never reaches the frontend, the
//! database, a log line or a command line.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Emitter};

use crate::{credentials, logic, resolve_ai_worker, resolve_python, storage::Database, AppState};

pub const DEFAULT_MODEL: &str = "gemini-3.8-flash";
pub const SUPPORTED_MODELS: [&str; 4] = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
];

const AI_IDLE_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const AI_MAX_RUNTIME: Duration = Duration::from_secs(45 * 60);
const TEST_TIMEOUT: Duration = Duration::from_secs(45);
const MAX_MESSAGE_BYTES: usize = 1_048_576;

pub struct ActiveAi {
    pub job_id: String,
    stdin: ChildStdin,
    pid: u32,
    cancelling: Arc<AtomicBool>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AiSettings {
    pub enabled: bool,
    pub model: String,
    pub max_windows: u32,
    pub consent_accepted: bool,
    pub has_key: bool,
    pub status: String,
    pub supported_models: Vec<String>,
    pub privacy_note: String,
}

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SaveAiSettings {
    pub enabled: bool,
    pub model: String,
    pub max_windows: u32,
    pub consent_accepted: bool,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AiConnectionResult {
    pub ok: bool,
    pub model: String,
    pub message: String,
    pub latency_ms: Option<u64>,
}

fn privacy_note() -> String {
    "Cloud AI analysis sends only the short video segments Luna selects to Google's \
     Gemini API. The free tier may use submitted content to improve Google's products. \
     Luna's local analysis always works without this feature."
        .to_string()
}

fn read_settings(database: &Database) -> Result<(bool, String, u32, bool), String> {
    let enabled = database.setting("ai.enabled")?.map(|value| value == "1").unwrap_or(false);
    let model = database.setting("ai.model")?.unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let max_windows = database
        .setting("ai.maxWindows")?
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or(6)
        .clamp(1, 12);
    let consent = database.setting("ai.consent")?.map(|value| value == "1").unwrap_or(false);
    Ok((enabled, model, max_windows, consent))
}

pub fn settings(state: &Arc<AppState>) -> Result<AiSettings, String> {
    let (enabled, model, max_windows, consent) = {
        let database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        read_settings(&database)?
    };
    let has_key = credentials::has_gemini_key();
    let status = if !enabled {
        "disabled"
    } else if !has_key {
        "missing_key"
    } else {
        "ready"
    };
    Ok(AiSettings {
        enabled,
        model,
        max_windows,
        consent_accepted: consent,
        has_key,
        status: status.to_string(),
        supported_models: SUPPORTED_MODELS.iter().map(|value| value.to_string()).collect(),
        privacy_note: privacy_note(),
    })
}

pub fn save_settings(state: &Arc<AppState>, request: SaveAiSettings) -> Result<AiSettings, String> {
    let model = request.model.trim();
    if model.is_empty() || model.len() > 64
        || !model.chars().all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '.' | '_'))
    {
        return Err("Choose one of the supported Gemini model names.".into());
    }
    if request.enabled && !request.consent_accepted {
        return Err("Accept the cloud processing notice before enabling Cloud AI analysis.".into());
    }
    {
        let mut database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        database.set_setting("ai.enabled", if request.enabled { "1" } else { "0" })?;
        database.set_setting("ai.model", model)?;
        database.set_setting("ai.maxWindows", &request.max_windows.clamp(1, 12).to_string())?;
        database.set_setting("ai.consent", if request.consent_accepted { "1" } else { "0" })?;
    }
    settings(state)
}

pub fn store_key(state: &Arc<AppState>, key: &str) -> Result<AiSettings, String> {
    credentials::store_gemini_key(key)?;
    settings(state)
}

pub fn clear_key(state: &Arc<AppState>) -> Result<AiSettings, String> {
    credentials::delete_gemini_key()?;
    settings(state)
}

/// Spawn the worker in test mode. Nothing is uploaded: the worker only calls the
/// model metadata endpoint to prove the key is valid.
pub fn test_connection(app: &AppHandle, state: &Arc<AppState>) -> Result<AiConnectionResult, String> {
    let model = {
        let database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        read_settings(&database)?.1
    };
    let key = credentials::load_gemini_key()?
        .ok_or_else(|| "Add your own Gemini API key in Cloud AI settings first.".to_string())?;
    let worker = resolve_ai_worker(app)?;
    let (python, python_args) = resolve_python()?;
    let mut command = Command::new(python);
    command
        .args(python_args)
        .arg(worker)
        .arg("--test-connection")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("LUNA_AI_MODEL", &model);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start the cloud AI worker: {error}"))?;
    if let Some(stdin) = child.stdin.as_mut() {
        let line = json!({ "apiKey": key }).to_string();
        let _ = stdin.write_all(line.as_bytes());
        let _ = stdin.write_all(b"\n");
        let _ = stdin.flush();
    }
    let stdout = child.stdout.take().ok_or_else(|| "Could not read the cloud AI worker output.".to_string())?;
    let (sender, receiver) = mpsc::sync_channel(4);
    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            if sender.send(line).is_err() {
                break;
            }
        }
    });
    let deadline = Instant::now() + TEST_TIMEOUT;
    let mut result: Option<AiConnectionResult> = None;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match receiver.recv_timeout(remaining) {
            Ok(line) => {
                let Ok(event) = serde_json::from_str::<Value>(&line) else { continue };
                if event.get("type").and_then(Value::as_str) != Some("test") {
                    continue;
                }
                let ok = event.get("ok").and_then(Value::as_bool).unwrap_or(false);
                result = Some(AiConnectionResult {
                    ok,
                    model: event.get("model").and_then(Value::as_str).unwrap_or(&model).to_string(),
                    message: if ok {
                        "Connected to Gemini with your key.".to_string()
                    } else {
                        event
                            .get("message")
                            .and_then(Value::as_str)
                            .unwrap_or("Gemini rejected the stored key.")
                            .to_string()
                    },
                    latency_ms: event.get("latencyMs").and_then(Value::as_u64),
                });
                break;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => break,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    result.ok_or_else(|| {
        "The cloud AI worker did not answer in time. Check your connection and try again.".to_string()
    })
}

pub fn start(app: &AppHandle, state: &Arc<AppState>, job_id: &str) -> Result<(), String> {
    let (enabled, model, max_windows, consent) = {
        let database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        read_settings(&database)?
    };
    if !enabled {
        return Err("Cloud AI analysis is turned off. Enable it in Cloud AI settings.".into());
    }
    if !consent {
        return Err("Accept the cloud processing notice before analysing with Gemini.".into());
    }
    let key = credentials::load_gemini_key()?
        .ok_or_else(|| "Add your own Gemini API key in Cloud AI settings to use cloud analysis.".to_string())?;
    {
        let running = state.ai.lock().map_err(|_| "Cloud AI lock is unavailable.".to_string())?;
        if running.is_some() {
            return Err("A cloud analysis is already running. Wait for it to finish.".into());
        }
    }
    let (job, candidates) = {
        let database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        let job = database
            .job_source(job_id)?
            .ok_or_else(|| "This analysis is no longer available.".to_string())?;
        if job.status != "completed" {
            return Err("Finish the local analysis before running cloud analysis.".into());
        }
        (job, database.candidates(job_id)?)
    };
    if candidates.is_empty() {
        return Err("There are no local candidates to review. Run the analysis again.".into());
    }
    let batch_id = logic::new_ai_id();
    let request_dir = state.data_dir.join("ai-requests");
    fs::create_dir_all(&request_dir)
        .map_err(|error| format!("Could not prepare the cloud request: {error}"))?;
    let request_path = request_dir.join(format!("{batch_id}.json"));
    let request = json!({
        "jobId": job_id,
        "batchId": batch_id,
        "source": job.path,
        "durationMs": job.duration_ms.max(0),
        "enabled": true,
        "model": model,
        "ai": { "max_windows": max_windows, "enabled": true },
        "candidates": candidates.iter().map(|candidate| json!({
            "id": candidate.id,
            "startMs": candidate.start_ms,
            "peakMs": candidate.peak_ms,
            "endMs": candidate.end_ms,
            "score": candidate.score,
            "signals": candidate.signals,
        })).collect::<Vec<_>>(),
    });
    fs::write(&request_path, serde_json::to_vec(&request).map_err(|error| error.to_string())?)
        .map_err(|error| format!("Could not write the cloud request: {error}"))?;

    let worker = resolve_ai_worker(app)?;
    let (python, python_args) = resolve_python()?;
    let mut command = Command::new(python);
    command
        .args(python_args)
        .arg(worker)
        .arg("--request")
        .arg(&request_path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
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
            return Err(format!("Could not start the cloud AI worker: {error}"));
        }
    };
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "Could not open the cloud worker control channel.".to_string())?;
    let line = json!({ "apiKey": key }).to_string();
    stdin
        .write_all(line.as_bytes())
        .and_then(|_| stdin.write_all(b"\n"))
        .and_then(|_| stdin.flush())
        .map_err(|error| format!("Could not send the API key to the worker: {error}"))?;
    let stdout = child.stdout.take().ok_or_else(|| "Could not read cloud progress.".to_string())?;
    let stderr = child.stderr.take().ok_or_else(|| "Could not read cloud diagnostics.".to_string())?;
    let cancelling = Arc::new(AtomicBool::new(false));
    let pid = child.id();
    {
        let mut slot = state.ai.lock().map_err(|_| "Cloud AI lock is unavailable.".to_string())?;
        if slot.is_some() {
            let _ = child.kill();
            let _ = fs::remove_file(&request_path);
            return Err("Another cloud analysis started first.".into());
        }
        *slot = Some(ActiveAi {
            job_id: job_id.to_string(),
            stdin,
            pid,
            cancelling: Arc::clone(&cancelling),
        });
    }
    {
        let mut database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        database.set_job_ai_status(job_id, "running", Some("Reviewing candidate windows"), Some(&model), Some(0))?;
    }
    let app_thread = app.clone();
    let state_thread = Arc::clone(state);
    let thread_job = job_id.to_string();
    thread::spawn(move || {
        monitor(app_thread, state_thread, thread_job, child, stdout, stderr, cancelling, request_path);
    });
    Ok(())
}

pub fn cancel(state: &Arc<AppState>, job_id: &str) -> Result<(), String> {
    let pid = {
        let mut slot = state.ai.lock().map_err(|_| "Cloud AI lock is unavailable.".to_string())?;
        let active = slot.as_mut().ok_or_else(|| "No cloud analysis is running.".to_string())?;
        if active.job_id != job_id {
            return Err("No cloud analysis is running for this analysis.".to_string());
        }
        active.cancelling.store(true, Ordering::SeqCst);
        serde_json::to_writer(&mut active.stdin, &json!({ "command": "cancel" }))
            .map_err(|error| format!("Could not cancel cloud analysis: {error}"))?;
        active
            .stdin
            .write_all(b"\n")
            .and_then(|_| active.stdin.flush())
            .map_err(|error| format!("Could not cancel cloud analysis: {error}"))?;
        active.pid
    };
    let state_clone = Arc::clone(state);
    let job = job_id.to_string();
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(6));
        let still_running = state_clone
            .ai
            .lock()
            .ok()
            .map(|slot| slot.as_ref().is_some_and(|active| active.job_id == job))
            .unwrap_or(false);
        if still_running {
            let _ = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
    });
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn monitor(app: AppHandle, state: Arc<AppState>, job_id: String, mut child: Child,
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
                    if sender.send(Ok(line)).is_err() {
                        break;
                    }
                }
                Ok(_) => {
                    let _ = sender.send(Err("Cloud worker message exceeded the size limit.".to_string()));
                    break;
                }
                Err(error) => {
                    let _ = sender.send(Err(format!("Could not read cloud progress: {error}")));
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
    let mut failure: Option<String> = None;
    let deadline = Instant::now() + AI_MAX_RUNTIME;
    let mut last_activity = Instant::now();
    loop {
        let remaining = AI_IDLE_TIMEOUT
            .saturating_sub(last_activity.elapsed())
            .min(deadline.saturating_duration_since(Instant::now()));
        let output = match receiver.recv_timeout(remaining) {
            Ok(output) => output,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                failure = Some(if last_activity.elapsed() >= AI_IDLE_TIMEOUT {
                    "Cloud analysis stopped responding. Local results are unchanged.".to_string()
                } else {
                    "Cloud analysis exceeded the time limit. Local results are unchanged.".to_string()
                });
                let _ = child.kill();
                break;
            }
        };
        let Ok(line) = output else {
            failure = output.err();
            let _ = child.kill();
            break;
        };
        last_activity = Instant::now();
        let Ok(event) = serde_json::from_slice::<Value>(&line) else {
            failure = Some("The cloud worker sent invalid data.".to_string());
            let _ = child.kill();
            break;
        };
        if event.get("jobId").and_then(Value::as_str) != Some(job_id.as_str()) {
            failure = Some("The cloud worker reported an unexpected analysis.".to_string());
            let _ = child.kill();
            break;
        }
        let kind = event.get("type").and_then(Value::as_str).unwrap_or_default();
        if let Ok(mut database) = state.database.lock() {
            let _ = apply_event(&mut database, &job_id, kind, &event);
        }
        terminal |= matches!(kind, "ai_complete" | "error");
        let _ = app.emit("ai-event", event);
    }

    drop(receiver);
    let _ = child.wait();
    let diagnostics = stderr_thread.join().unwrap_or_default();
    let cancelled = cancelling.load(Ordering::SeqCst);
    if !terminal {
        let message = failure.unwrap_or_else(|| {
            if cancelled {
                "Cloud analysis was stopped. Local results are unchanged.".to_string()
            } else if diagnostics.trim().is_empty() {
                "Cloud analysis stopped before finishing. Local results are unchanged.".to_string()
            } else {
                format!("Cloud analysis stopped: {}", diagnostics.trim())
            }
        });
        if let Ok(mut database) = state.database.lock() {
            let status = if cancelled { "cancelled" } else { "failed" };
            let _ = database.set_job_ai_status(&job_id, status, Some(&message), None, None);
        }
        let event = json!({
            "type": "ai_complete",
            "jobId": job_id,
            "cancelled": cancelled,
            "failed": 0,
            "assessed": 0,
            "message": message,
        });
        let _ = app.emit("ai-event", event);
    }
    if let Ok(mut slot) = state.ai.lock() {
        if slot.as_ref().is_some_and(|active| active.job_id == job_id) {
            *slot = None;
        }
    }
    let _ = fs::remove_file(&request_path);
}

fn apply_event(database: &mut Database, job_id: &str, kind: &str, event: &Value) -> Result<(), String> {
    match kind {
        "ai_started" => {
            let total = event.get("total").and_then(Value::as_i64);
            let model = event.get("model").and_then(Value::as_str);
            database.set_job_ai_status(job_id, "running", Some("Reviewing candidate windows"), model, total)?;
        }
        "ai_item" => {
            if let Some(item) = event.get("item") {
                database.apply_ai_item(job_id, item)?;
            }
        }
        "ai_complete" => {
            let cancelled = event.get("cancelled").and_then(Value::as_bool).unwrap_or(false);
            let rate_limited = event.get("rateLimited").and_then(Value::as_bool).unwrap_or(false);
            let assessed = event.get("assessed").and_then(Value::as_i64).unwrap_or(0);
            let failed = event.get("failed").and_then(Value::as_i64).unwrap_or(0);
            let rejected = event.get("rejected").and_then(Value::as_i64).unwrap_or(0);
            let total = event.get("total").and_then(Value::as_i64);
            let message = event.get("message").and_then(Value::as_str).map(str::to_string);
            let (status, summary) = if cancelled {
                ("cancelled", message.unwrap_or_else(|| {
                    "Cloud analysis was stopped. Local results are unchanged.".to_string()
                }))
            } else if rate_limited {
                ("rate_limited", message.unwrap_or_else(|| {
                    "Gemini rate limit reached. Try again later; local results are unchanged.".to_string()
                }))
            } else if assessed == 0 {
                ("failed", message.unwrap_or_else(|| {
                    "Gemini returned no usable assessments. Local results are unchanged.".to_string()
                }))
            } else {
                let mut text = format!(
                    "Cloud AI reviewed {assessed} of {} window(s)",
                    total.unwrap_or(assessed)
                );
                if failed > 0 || rejected > 0 {
                    text.push_str(&format!(", {failed} failed and {rejected} rejected"));
                }
                ("completed", text)
            };
            database.set_job_ai_status(job_id, status, Some(&summary), None, total)?;
        }
        "error" => {
            let message = event.get("message").and_then(Value::as_str)
                .unwrap_or("Cloud analysis failed. Local results are unchanged.");
            database.set_job_ai_status(job_id, "failed", Some(message), None, None)?;
        }
        "ai_progress" => {}
        _ => {}
    }
    Ok(())
}

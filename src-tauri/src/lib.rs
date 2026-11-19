mod ai;
mod credentials;
mod exports;
mod logic;
mod storage;

use serde::Serialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
use tauri::{path::BaseDirectory, AppHandle, Emitter, Manager, State};

use storage::{Database, HistoryEntry};

const MAX_WORKER_MESSAGE_BYTES: usize = 1_048_576;
const MAX_WORKER_RUNTIME: Duration = Duration::from_secs(12 * 60 * 60);
const DEFAULT_ANALYSIS_CONFIG: &str = include_str!("../../worker/config.json");

pub(crate) struct AppState {
    pub(crate) database: Mutex<Database>,
    pub(crate) workers: Mutex<HashMap<String, ChildStdin>>,
    pub(crate) export: Mutex<Option<exports::ActiveExport>>,
    pub(crate) ai: Mutex<Option<ai::ActiveAi>>,
    pub(crate) data_dir: PathBuf,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct MediaInfo {
    path: String,
    name: String,
    duration_seconds: f64,
    size_bytes: u64,
    width: Option<u64>,
    height: Option<u64>,
    display_width: Option<u64>,
    display_height: Option<u64>,
    rotation: i64,
    frame_rate: Option<String>,
    video_codec: Option<String>,
    audio_codec: Option<String>,
    audio_tracks: u64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CaptionData {
    available: bool,
    has_transcript: bool,
    segments: Vec<Value>,
}

#[tauri::command]
async fn register_media(app: AppHandle, state: State<'_, Arc<AppState>>, path: String) -> Result<MediaInfo, String> {
    let state = Arc::clone(state.inner());
    tauri::async_runtime::spawn_blocking(move || {
        let media = inspect_media(&path)?;
        app.asset_protocol_scope().allow_file(&media.path)
            .map_err(|error| format!("The preview could not access this recording: {error}"))?;
        state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
            .register_source(&media)?;
        Ok(media)
    }).await.map_err(|error| format!("Media inspection task failed: {error}"))?
}

#[tauri::command]
fn list_history(state: State<'_, Arc<AppState>>) -> Result<Vec<HistoryEntry>, String> {
    state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?.history()
}

#[tauri::command]
fn get_caption_data(state: State<'_, Arc<AppState>>, job_id: String, start_ms: u64, end_ms: u64)
    -> Result<CaptionData, String> {
    let segments = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .transcript(&job_id)?;
    let has_transcript = !segments.is_empty();
    let start_seconds = start_ms as f64 / 1000.0;
    let end_seconds = end_ms as f64 / 1000.0;
    let filtered = segments.into_iter().filter(|segment| {
        let segment_start = segment.get("start_seconds").and_then(Value::as_f64).unwrap_or(0.0);
        let segment_end = segment.get("end_seconds").and_then(Value::as_f64).unwrap_or(segment_start);
        segment_end >= start_seconds - 2.0 && segment_start <= end_seconds + 1.0
    }).collect::<Vec<_>>();
    Ok(CaptionData { available: !filtered.is_empty(), has_transcript, segments: filtered })
}

#[tauri::command]
fn start_short_export(app: AppHandle, state: State<'_, Arc<AppState>>, request: exports::ExportRequest)
    -> Result<exports::StartExportResult, String> {
    exports::start(&app, state.inner(), request)
}

#[tauri::command]
fn retry_short_export(app: AppHandle, state: State<'_, Arc<AppState>>, export_id: String)
    -> Result<exports::StartExportResult, String> {
    exports::retry(&app, state.inner(), &export_id)
}

#[tauri::command]
fn cancel_short_export(state: State<'_, Arc<AppState>>, batch_id: String) -> Result<(), String> {
    exports::cancel(state.inner(), &batch_id)
}

#[tauri::command]
fn list_exports(state: State<'_, Arc<AppState>>, job_id: Option<String>, limit: Option<u32>)
    -> Result<Vec<storage::ExportRecord>, String> {
    state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .list_exports(job_id.as_deref(), limit.unwrap_or(30))
}

#[tauri::command]
fn open_short_export(state: State<'_, Arc<AppState>>, export_id: String) -> Result<(), String> {
    exports::open_export(state.inner(), &export_id, false)
}

#[tauri::command]
fn reveal_short_export(state: State<'_, Arc<AppState>>, export_id: String) -> Result<(), String> {
    exports::open_export(state.inner(), &export_id, true)
}

#[tauri::command]
fn ai_settings(state: State<'_, Arc<AppState>>) -> Result<ai::AiSettings, String> {
    ai::settings(state.inner())
}

#[tauri::command]
fn save_ai_settings(state: State<'_, Arc<AppState>>, request: ai::SaveAiSettings)
    -> Result<ai::AiSettings, String> {
    ai::save_settings(state.inner(), request)
}

#[tauri::command]
fn set_gemini_key(state: State<'_, Arc<AppState>>, key: String) -> Result<ai::AiSettings, String> {
    ai::store_key(state.inner(), &key)
}

#[tauri::command]
fn clear_gemini_key(state: State<'_, Arc<AppState>>) -> Result<ai::AiSettings, String> {
    ai::clear_key(state.inner())
}

#[tauri::command]
fn test_ai_connection(app: AppHandle, state: State<'_, Arc<AppState>>)
    -> Result<ai::AiConnectionResult, String> {
    ai::test_connection(&app, state.inner())
}

#[tauri::command]
fn start_ai_analysis(app: AppHandle, state: State<'_, Arc<AppState>>, job_id: String)
    -> Result<(), String> {
    ai::start(&app, state.inner(), &job_id)
}

#[tauri::command]
fn cancel_ai_analysis(state: State<'_, Arc<AppState>>, job_id: String) -> Result<(), String> {
    ai::cancel(state.inner(), &job_id)
}

#[tauri::command]
fn start_analysis(app: AppHandle, state: State<'_, Arc<AppState>>, path: String) -> Result<String, String> {
    let mut active_workers = state.workers.lock().map_err(|_| "Worker lock is unavailable.".to_string())?;
    if !active_workers.is_empty() {
        return Err("An analysis is already running. Wait for it to finish or cancel it first.".into());
    }
    let worker_path = resolve_worker(&app)?;
    let (python, python_args) = resolve_python()?;
    let (source_id, duration_seconds) = {
        let database = state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?;
        let source_id = database.source_id(&path)?
            .ok_or_else(|| "Import this recording before starting analysis.".to_string())?;
        (source_id, database.source_duration_seconds(source_id)?)
    };
    let output_dir = clip_output_dir(&app, &path)?;
    let idle_timeout = worker_idle_timeout(duration_seconds);
    let job_id = new_job_id();
    state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .create_job(&job_id, source_id, DEFAULT_ANALYSIS_CONFIG)?;

    let mut command = Command::new(python);
    command.args(python_args)
        .arg(worker_path)
        .arg("--path").arg(&path)
        .arg("--output-dir").arg(&output_dir)
        .arg("--job-id").arg(&job_id)
        .arg("--config-json").arg(DEFAULT_ANALYSIS_CONFIG)
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
            let message = format!("Could not start Python. Install Python 3.11 or newer or set LUNA_PYTHON to its executable: {error}");
            let _ = state.database.lock().map(|mut db| db.set_job_failed(&job_id, "python_missing", &message));
            return Err(message);
        }
    };
    let stdin = child.stdin.take().ok_or_else(|| "Could not open worker control channel.".to_string())?;
    let stdout = child.stdout.take().ok_or_else(|| "Could not read worker output.".to_string())?;
    let stderr = child.stderr.take().ok_or_else(|| "Could not read worker diagnostics.".to_string())?;
    active_workers.insert(job_id.clone(), stdin);

    let app_thread = app.clone();
    let state_thread = Arc::clone(state.inner());
    let thread_job_id = job_id.clone();
    thread::spawn(move || {
        monitor_worker(app_thread, state_thread, thread_job_id, child, stdout, stderr, idle_timeout);
    });
    Ok(job_id)
}

#[tauri::command]
fn cancel_analysis(state: State<'_, Arc<AppState>>, job_id: String) -> Result<(), String> {
    let mut workers = state.workers.lock().map_err(|_| "Worker lock is unavailable.".to_string())?;
    let input = workers.get_mut(&job_id).ok_or_else(|| "This analysis is no longer running.".to_string())?;
    serde_json::to_writer(&mut *input, &json!({"command":"cancel","job_id":job_id}))
        .map_err(|error| format!("Could not send cancellation: {error}"))?;
    input.write_all(b"\n").and_then(|_| input.flush()).map_err(|error| format!("Could not send cancellation: {error}"))?;
    state.database.lock().map_err(|_| "Database lock is unavailable.".to_string())?
        .mark_cancelling(&job_id)?;
    Ok(())
}

fn inspect_media(path: &str) -> Result<MediaInfo, String> {
    let file = PathBuf::from(path);
    validate_file(&file)?;
    let canonical = file.canonicalize().map_err(|_| "This recording cannot be opened.".to_string())?;
    let output = Command::new("ffprobe")
        .args([
            "-v", "error", "-show_streams", "-show_format", "-of", "json",
        ])
        .arg(&canonical)
        .stdin(Stdio::null())
        .output()
        .map_err(|_| "FFprobe was not found. Install FFmpeg and make ffprobe available on PATH.".to_string())?;
    if !output.status.success() {
        return Err("FFprobe could not read this recording. It may be damaged or unsupported.".into());
    }
    let data: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "FFprobe returned invalid media metadata.".to_string())?;
    let format = data.get("format").unwrap_or(&Value::Null);
    let duration_seconds = format.get("duration").and_then(Value::as_str)
        .and_then(|value| value.parse::<f64>().ok())
        .ok_or_else(|| "This recording has no readable duration.".to_string())?;
    if !duration_seconds.is_finite() || duration_seconds <= 0.0 {
        return Err("This recording has an invalid duration.".into());
    }
    let streams = data.get("streams").and_then(Value::as_array).cloned().unwrap_or_default();
    let video = streams.iter().find(|stream| stream.get("codec_type").and_then(Value::as_str) == Some("video"));
    let audio_streams: Vec<_> = streams.iter()
        .filter(|stream| stream.get("codec_type").and_then(Value::as_str) == Some("audio"))
        .collect();
    let video = video.ok_or_else(|| "This file does not contain a readable video track.".to_string())?;
    let name = canonical.file_name().and_then(|value| value.to_str()).unwrap_or("Recording").to_string();
    let size_bytes = fs::metadata(&canonical).map_err(|_| "Could not read recording details.".to_string())?.len();
    let width = video.get("width").and_then(Value::as_u64);
    let height = video.get("height").and_then(Value::as_u64);
    let rotation = video.get("side_data_list").and_then(Value::as_array)
        .and_then(|items| items.iter().find_map(|item| item.get("rotation").and_then(Value::as_f64)))
        .or_else(|| video.get("tags").and_then(|tags| tags.get("rotate")).and_then(Value::as_str)
            .and_then(|value| value.parse::<f64>().ok()))
        .map(|value| value.round() as i64)
        .unwrap_or(0);
    let (display_width, display_height) = if rotation.abs() % 180 == 90 {
        (height, width)
    } else {
        (width, height)
    };

    Ok(MediaInfo {
        path: canonical.to_string_lossy().to_string(),
        name,
        duration_seconds,
        size_bytes,
        width,
        height,
        display_width,
        display_height,
        rotation,
        frame_rate: video.get("avg_frame_rate").and_then(Value::as_str).map(normalize_rate),
        video_codec: video.get("codec_name").and_then(Value::as_str).map(str::to_string),
        audio_codec: audio_streams.first().and_then(|stream| stream.get("codec_name")).and_then(Value::as_str).map(str::to_string),
        audio_tracks: audio_streams.len() as u64,
    })
}

fn validate_file(path: &Path) -> Result<(), String> {
    if !path.is_file() {
        return Err("Select an existing video file.".into());
    }
    let extension = path.extension().and_then(|value| value.to_str()).unwrap_or_default().to_ascii_lowercase();
    if !supported_extension(&extension) {
        return Err("Supported recordings are MP4, MKV and WebM.".into());
    }
    fs::File::open(path).map_err(|_| "This recording cannot be read.".to_string())?;
    Ok(())
}

pub use logic::{normalize_rate, supported_extension};

pub(crate) fn resolve_worker(app: &AppHandle) -> Result<PathBuf, String> {
    if cfg!(debug_assertions) {
        let development_path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../worker/clipper_worker.py");
        if development_path.is_file() {
            return Ok(development_path);
        }
    }
    app.path().resolve("worker/clipper_worker.py", BaseDirectory::Resource)
        .map_err(|error| format!("The bundled Python worker could not be located: {error}"))
}

pub(crate) fn resolve_export_worker(app: &AppHandle) -> Result<PathBuf, String> {
    let worker = resolve_worker(app)?;
    let export_worker = worker.with_file_name("export_worker.py");
    if export_worker.is_file() {
        Ok(export_worker)
    } else {
        Err("The bundled short-form export worker could not be located.".into())
    }
}

pub(crate) fn resolve_ai_worker(app: &AppHandle) -> Result<PathBuf, String> {
    let worker = resolve_worker(app)?;
    let ai_worker = worker.with_file_name("ai_worker.py");
    if ai_worker.is_file() {
        Ok(ai_worker)
    } else {
        Err("The bundled cloud AI worker could not be located.".into())
    }
}

fn safe_folder_name(media_path: &str) -> String {
    let stem = Path::new(media_path).file_stem().and_then(|name| name.to_str()).unwrap_or("Luna clips");
    let safe_name: String = stem.chars()
        .map(|character| if character.is_control() || "<>:\"/\\|?*".contains(character) { '_' } else { character })
        .collect();
    let safe_name = safe_name.trim_matches([' ', '.']);
    let safe_name = if safe_name.is_empty() { "Luna clips" } else { safe_name };
    let reserved = safe_name.split('.').next().unwrap_or("").to_ascii_uppercase();
    if matches!(reserved.as_str(), "CON" | "PRN" | "AUX" | "NUL" | "COM1" | "COM2" | "COM3" | "COM4" | "COM5" | "COM6" | "COM7" | "COM8" | "COM9" | "LPT1" | "LPT2" | "LPT3" | "LPT4" | "LPT5" | "LPT6" | "LPT7" | "LPT8" | "LPT9") {
        format!("_{safe_name}")
    } else {
        safe_name.to_string()
    }
}

fn clip_output_dir(app: &AppHandle, media_path: &str) -> Result<PathBuf, String> {
    let downloads = app.path().download_dir()
        .map_err(|error| format!("Could not find the Downloads folder: {error}"))?;
    let folder = downloads.join(safe_folder_name(media_path));
    fs::create_dir_all(&folder).map_err(|error| format!("Could not create the clip folder in Downloads: {error}"))?;
    Ok(folder)
}

fn short_export_dir(app: &AppHandle, media_path: &str) -> Result<PathBuf, String> {
    let downloads = app.path().download_dir()
        .map_err(|error| format!("Could not find the Downloads folder: {error}"))?;
    let folder = downloads.join("Luna Shorts").join(safe_folder_name(media_path));
    fs::create_dir_all(&folder)
        .map_err(|error| format!("Could not create the short-form export folder: {error}"))?;
    Ok(folder)
}

#[tauri::command]
fn default_export_dir(app: AppHandle, source_path: String) -> Result<String, String> {
    short_export_dir(&app, &source_path).map(|folder| folder.to_string_lossy().to_string())
}

pub(crate) fn resolve_python() -> Result<(PathBuf, Vec<std::ffi::OsString>), String> {
    let explicit = std::env::var_os("LUNA_PYTHON");
    let mut candidates: Vec<(PathBuf, Vec<std::ffi::OsString>)> = Vec::new();
    if let Some(path) = explicit {
        candidates.push((PathBuf::from(path), Vec::new()));
    } else {
        let project_python = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.venv/Scripts/python.exe");
        if project_python.is_file() {
            candidates.push((project_python, Vec::new()));
        }
        if let Some(local_app_data) = std::env::var_os("LOCALAPPDATA") {
            let local_app_data = PathBuf::from(local_app_data);
            candidates.push((local_app_data.join("Python/bin/python.exe"), Vec::new()));
            let installs = local_app_data.join("Programs/Python");
            if let Ok(entries) = fs::read_dir(installs) {
                let mut interpreters: Vec<_> = entries.flatten()
                    .map(|entry| entry.path().join("python.exe"))
                    .filter(|path| path.is_file())
                    .collect();
                interpreters.sort_by(|left, right| right.cmp(left));
                candidates.extend(interpreters.into_iter().map(|path| (path, Vec::new())));
            }
        }
        candidates.push((PathBuf::from("py"), vec!["-3".into()]));
        candidates.push((PathBuf::from("python3"), Vec::new()));
        candidates.push((PathBuf::from("python"), Vec::new()));
    }

    for (executable, args) in candidates {
        let Ok(output) = Command::new(&executable).args(&args).arg("--version").output() else {
            continue;
        };
        if !output.status.success() {
            continue;
        }
        let version = format!("{} {}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
        let supported = version.split_whitespace()
            .find_map(|part| part.strip_prefix("3.")?.split('.').next()?.parse::<u32>().ok())
            .is_some_and(|minor| minor >= 11);
        if supported {
            return Ok((executable, args));
        }
    }
    Err("Python 3.11 or newer was not found. Install Python or set LUNA_PYTHON to its executable.".into())
}

pub use logic::{new_job_id, worker_idle_timeout};

fn monitor_worker(app: AppHandle, state: Arc<AppState>, job_id: String, mut child: Child,
                  stdout: impl Read + Send + 'static, stderr: impl Read + Send + 'static,
                  idle_timeout: Duration) {
    let (output_sender, output_receiver) = mpsc::sync_channel(32);
    let stdout_thread = thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            let mut line = Vec::new();
            match reader.by_ref().take((MAX_WORKER_MESSAGE_BYTES + 1) as u64).read_until(b'\n', &mut line) {
                Ok(0) => break,
                Ok(length) if length <= MAX_WORKER_MESSAGE_BYTES && line.last() == Some(&b'\n') => {
                    if output_sender.send(Ok(line)).is_err() { break; }
                }
                Ok(_) => {
                    let _ = output_sender.send(Err("Python worker message exceeded the size limit or was truncated.".to_string()));
                    break;
                }
                Err(error) => {
                    let _ = output_sender.send(Err(format!("Could not read Python worker output: {error}")));
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
    let deadline = Instant::now() + MAX_WORKER_RUNTIME;
    let mut last_activity = Instant::now();
    let mut last_stage = "starting the Python worker".to_string();
    loop {
        let remaining = idle_timeout.saturating_sub(last_activity.elapsed())
            .min(deadline.saturating_duration_since(Instant::now()));
        let output = match output_receiver.recv_timeout(remaining) {
            Ok(output) => output,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                invalid_message = Some(if last_activity.elapsed() >= idle_timeout {
                    format!("The analysis worker stopped responding during {last_stage}. Try the recording again.")
                } else {
                    format!("Analysis exceeded the {} hour time limit.", MAX_WORKER_RUNTIME.as_secs() / 3600)
                });
                terminal = false;
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
                        invalid_message = Some(format!("Python worker sent invalid JSON: {error}"));
                        let _ = child.kill();
                        break;
                    }
                };
                if event.get("job_id").and_then(Value::as_str) != Some(job_id.as_str()) {
                    invalid_message = Some("Python worker sent an event for an unexpected job.".to_string());
                    let _ = child.kill();
                    break;
                }
                let kind = event.get("type").and_then(Value::as_str).unwrap_or_default();
                last_stage = event.get("stage").and_then(Value::as_str).unwrap_or(kind).to_string();
                let update = state.database.lock()
                    .map_err(|_| "Database lock is unavailable.".to_string())
                    .and_then(|mut database| database.update_event(&event));
                if let Err(error) = update {
                    invalid_message = Some(error);
                    let _ = child.kill();
                    break;
                }
                terminal |= matches!(kind, "complete" | "error" | "cancelled");
                let _ = app.emit("analysis-event", event);
            }
            Err(error) => {
                invalid_message = Some(error);
                let _ = child.kill();
                break;
            }
        }
    }
    drop(output_receiver);
    let exit = child.wait();
    let _ = stdout_thread.join();
    let diagnostics = stderr_thread.join().unwrap_or_default();
    if !terminal {
        let message = invalid_message.unwrap_or_else(|| {
            if diagnostics.trim().is_empty() {
                format!("Python worker exited unexpectedly ({exit:?}).")
            } else {
                format!("Python worker exited unexpectedly: {}", diagnostics.trim())
            }
        });
        let event = json!({"job_id":job_id,"type":"error","code":"worker_stopped","message":message});
        if let Ok(mut database) = state.database.lock() {
            let _ = database.update_event(&event);
        }
        let _ = app.emit("analysis-event", event);
    }
    if let Ok(mut workers) = state.workers.lock() {
        workers.remove(&job_id);
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let data_dir = match std::env::var_os("LUNA_DATA_DIR") {
                Some(path) => PathBuf::from(path),
                None => app.path().app_data_dir()?,
            };
            fs::create_dir_all(&data_dir)?;
            let data_path = data_dir.join("luna.sqlite3");
            let database = Database::open(&data_path).map_err(std::io::Error::other)?;
            app.manage(Arc::new(AppState {
                database: Mutex::new(database),
                workers: Mutex::new(HashMap::new()),
                export: Mutex::new(None),
                ai: Mutex::new(None),
                data_dir,
            }));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            register_media, list_history, start_analysis, cancel_analysis,
            get_caption_data, default_export_dir, start_short_export, retry_short_export,
            cancel_short_export, list_exports, open_short_export, reveal_short_export,
            ai_settings, save_ai_settings, set_gemini_key, clear_gemini_key,
            test_ai_connection, start_ai_analysis, cancel_ai_analysis,
        ])
        .run(tauri::generate_context!())
        .expect("Luna desktop runtime failed");
}

use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub const MIN_WORKER_IDLE_TIMEOUT: Duration = Duration::from_secs(120);

static JOB_COUNTER: AtomicU64 = AtomicU64::new(1);

/// Allow slow model initialization and long recordings without letting a dead
/// worker hang the desktop app. The floor covers Whisper/VAD startup; longer
/// recordings get 7% of their duration as idle allowance.
pub fn worker_idle_timeout(duration_seconds: f64) -> Duration {
    Duration::from_secs_f64((duration_seconds.max(0.0) * 0.07).max(MIN_WORKER_IDLE_TIMEOUT.as_secs_f64()))
}

pub fn supported_extension(extension: &str) -> bool {
    ["mp4", "mkv", "webm"].contains(&extension.to_ascii_lowercase().as_str())
}

pub fn normalize_rate(rate: &str) -> String {
    let mut parts = rate.split('/');
    match (
        parts.next().and_then(|value| value.parse::<f64>().ok()),
        parts.next().and_then(|value| value.parse::<f64>().ok()),
    ) {
        (Some(numerator), Some(denominator)) if denominator > 0.0 => {
            format!("{:.2} fps", numerator / denominator)
        }
        _ => rate.to_string(),
    }
}

pub fn new_job_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let sequence = JOB_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{}-{nanos}-{sequence}", std::process::id())
}

fn new_prefixed_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let sequence = JOB_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{prefix}-{nanos}-{sequence}")
}

pub fn new_batch_id() -> String {
    new_prefixed_id("batch")
}

pub fn new_export_id() -> String {
    new_prefixed_id("short")
}

pub const MIN_CLIP_SECONDS: f64 = 1.0;
pub const MAX_CLIP_SECONDS: f64 = 180.0;

/// Validate and normalize short-form export settings. `original` output always
/// uses the untouched source framing, regardless of the selected mode.
pub fn validate_export_settings(aspect: &str, mode: &str, crop_x: f64, crop_y: f64, crf: u8)
    -> Result<(String, String), String> {
    if !matches!(aspect, "vertical" | "square" | "original") {
        return Err("Choose a valid output format.".into());
    }
    if !matches!(mode, "full_frame" | "crop" | "original") {
        return Err("Choose a valid presentation mode.".into());
    }
    if !crop_x.is_finite() || !crop_y.is_finite() || !(0.0..=1.0).contains(&crop_x) || !(0.0..=1.0).contains(&crop_y) {
        return Err("Crop positions must be between 0% and 100%.".into());
    }
    if !(14..=28).contains(&crf) {
        return Err("Video quality must use a CRF between 14 and 28.".into());
    }
    let mode = if aspect == "original" { "original".to_string() } else { mode.to_string() };
    Ok((aspect.to_string(), mode))
}

/// Validate an edited clip range against the source duration. Ranges are never
/// silently truncated; the caller must present the error.
pub fn validate_clip_range(start_ms: u64, end_ms: u64, duration_ms: u64) -> Result<(), String> {
    if duration_ms == 0 {
        return Err("This recording has no readable duration.".into());
    }
    if start_ms >= duration_ms {
        return Err("The clip start must be before the end of the recording.".into());
    }
    if end_ms <= start_ms {
        return Err("The clip end must be after its start.".into());
    }
    if end_ms > duration_ms {
        return Err("The clip end cannot be later than the recording.".into());
    }
    let seconds = (end_ms - start_ms) as f64 / 1000.0;
    if seconds < MIN_CLIP_SECONDS - 0.001 {
        return Err("Short-form clips must be at least 1 second long.".into());
    }
    if seconds > MAX_CLIP_SECONDS + 0.001 {
        return Err("Short-form clips are limited to 180 seconds.".into());
    }
    Ok(())
}

pub fn export_is_terminal(status: &str) -> bool {
    matches!(status, "completed" | "failed" | "cancelled")
}

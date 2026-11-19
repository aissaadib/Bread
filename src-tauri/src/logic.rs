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

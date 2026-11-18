use serde::Serialize;
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct MediaInfo {
    path: String,
    name: String,
    duration_seconds: f64,
    size_bytes: u64,
    width: Option<u64>,
    height: Option<u64>,
    frame_rate: Option<String>,
    video_codec: Option<String>,
    audio_codec: Option<String>,
}

#[tauri::command]
fn inspect_media(path: String) -> Result<MediaInfo, String> {
    let file = PathBuf::from(&path);
    validate_file(&file)?;

    let output = Command::new("ffprobe")
        .args([
            "-v", "error", "-show_entries",
            "format=duration,size:format_tags=title:stream=codec_type,codec_name,width,height,avg_frame_rate",
            "-of", "json",
        ])
        .arg(&file)
        .stdin(Stdio::null())
        .output()
        .map_err(|_| "FFprobe was not found. Install FFmpeg and make ffprobe available on PATH.".to_string())?;

    if !output.status.success() {
        return Err("FFprobe could not read this recording. The file may be damaged or unsupported.".into());
    }

    let data: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "FFprobe returned invalid media metadata.".to_string())?;
    let format = data.get("format").unwrap_or(&Value::Null);
    let duration_seconds = format.get("duration")
        .and_then(Value::as_str)
        .and_then(|v| v.parse::<f64>().ok())
        .ok_or_else(|| "This recording has no readable duration.".to_string())?;
    let streams = data.get("streams").and_then(Value::as_array).cloned().unwrap_or_default();
    let video = streams.iter().find(|s| s.get("codec_type").and_then(Value::as_str) == Some("video"));
    let audio = streams.iter().find(|s| s.get("codec_type").and_then(Value::as_str) == Some("audio"));
    let filename = file.file_name().and_then(|v| v.to_str()).unwrap_or("Recording").to_string();
    let size_bytes = fs::metadata(&file).map_err(|_| "Could not read recording details.".to_string())?.len();

    Ok(MediaInfo {
        path,
        name: filename,
        duration_seconds,
        size_bytes,
        width: video.and_then(|s| s.get("width")).and_then(Value::as_u64),
        height: video.and_then(|s| s.get("height")).and_then(Value::as_u64),
        frame_rate: video.and_then(|s| s.get("avg_frame_rate")).and_then(Value::as_str).map(normalize_rate),
        video_codec: video.and_then(|s| s.get("codec_name")).and_then(Value::as_str).map(str::to_string),
        audio_codec: audio.and_then(|s| s.get("codec_name")).and_then(Value::as_str).map(str::to_string),
    })
}

fn validate_file(path: &Path) -> Result<(), String> {
    if !path.is_file() {
        return Err("Select an existing video file.".into());
    }
    if !is_supported_extension(path) {
        return Err("Supported recordings are MP4, MKV and WebM.".into());
    }
    fs::File::open(path).map_err(|_| "This recording cannot be read.".to_string())?;
    Ok(())
}

fn is_supported_extension(path: &Path) -> bool {
    let extension = path.extension().and_then(|v| v.to_str()).unwrap_or_default().to_ascii_lowercase();
    ["mp4", "mkv", "webm"].contains(&extension.as_str())
}

fn normalize_rate(rate: &str) -> String {
    let mut parts = rate.split('/');
    match (parts.next().and_then(|v| v.parse::<f64>().ok()), parts.next().and_then(|v| v.parse::<f64>().ok())) {
        (Some(numerator), Some(denominator)) if denominator > 0.0 => format!("{:.2} fps", numerator / denominator),
        _ => rate.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::{is_supported_extension, normalize_rate};
    use std::path::Path;

    #[test]
    fn supported_media_extensions_are_case_insensitive() {
        for path in ["capture.mp4", "capture.MKV", "capture.webm"] {
            assert!(is_supported_extension(Path::new(path)));
        }
        for path in ["capture.mov", "capture", "capture.mp4.exe"] {
            assert!(!is_supported_extension(Path::new(path)));
        }
    }

    #[test]
    fn frame_rates_are_rendered_as_frames_per_second() {
        assert_eq!(normalize_rate("30000/1001"), "29.97 fps");
        assert_eq!(normalize_rate("24/1"), "24.00 fps");
        assert_eq!(normalize_rate("unknown"), "unknown");
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![inspect_media])
        .run(tauri::generate_context!())
        .expect("Luna desktop runtime failed");
}

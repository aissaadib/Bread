//! Runs the desktop crate's Tauri-free logic tests without linking Tauri.
//!
//! The Windows GNU toolchain on the development machine cannot produce a
//! loadable test harness for Tauri-linked binaries (STATUS_ENTRYPOINT_NOT_FOUND),
//! while the desktop binary itself links and runs. This crate compiles the same
//! `src/logic.rs` source file so the pure functions stay covered by `cargo test`.

#[path = "../src/logic.rs"]
mod logic;

/// `storage.rs` refers to this type for source registration. It is defined here
/// (instead of pulling in `lib.rs`, which needs Tauri) so the storage layer's
/// real SQL can be tested without linking the desktop runtime.
pub struct MediaInfo {
    pub path: String,
    pub name: String,
    pub duration_seconds: f64,
    pub size_bytes: u64,
    pub width: Option<u64>,
    pub height: Option<u64>,
    pub frame_rate: Option<String>,
    pub video_codec: Option<String>,
    pub audio_codec: Option<String>,
    pub audio_tracks: u64,
}

#[path = "../src/storage.rs"]
mod storage;

#[cfg(test)]
mod tests {
    use super::logic::{
        export_is_terminal, new_batch_id, new_export_id, new_job_id, normalize_rate,
        supported_extension, validate_clip_range, validate_export_settings, worker_idle_timeout,
    };
    use std::path::Path;
    use std::time::Duration;

    #[test]
    fn supported_media_extensions_are_case_insensitive() {
        for path in ["capture.mp4", "capture.MKV", "capture.webm"] {
            let extension = Path::new(path)
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or_default();
            assert!(supported_extension(extension));
        }
        for path in ["capture.mov", "capture", "capture.mp4.exe"] {
            let extension = Path::new(path)
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or_default();
            assert!(!supported_extension(extension));
        }
    }

    #[test]
    fn frame_rates_are_formatted_and_job_ids_are_distinct() {
        assert_eq!(normalize_rate("30000/1001"), "29.97 fps");
        assert_eq!(normalize_rate("24/1"), "24.00 fps");
        assert_eq!(normalize_rate("unknown"), "unknown");
        assert_ne!(new_job_id(), new_job_id());
    }

    #[test]
    fn worker_idle_timeout_covers_model_startup_and_long_recordings() {
        assert_eq!(worker_idle_timeout(0.0), Duration::from_secs(120));
        assert_eq!(worker_idle_timeout(36.7), Duration::from_secs(120));
        assert_eq!(worker_idle_timeout(3_600.0), Duration::from_secs(252));
        assert_eq!(worker_idle_timeout(10_800.0), Duration::from_secs(756));
    }

    #[test]
    fn short_form_settings_are_validated_and_original_forces_source_framing() {
        assert_eq!(
            validate_export_settings("vertical", "full_frame", 0.5, 0.5, 21).unwrap(),
            ("vertical".to_string(), "full_frame".to_string())
        );
        assert_eq!(
            validate_export_settings("original", "crop", 0.5, 0.5, 21).unwrap(),
            ("original".to_string(), "original".to_string())
        );
        assert!(validate_export_settings("portrait", "crop", 0.5, 0.5, 21).is_err());
        assert!(validate_export_settings("square", "zoom", 0.5, 0.5, 21).is_err());
        assert!(validate_export_settings("vertical", "crop", 1.2, 0.5, 21).is_err());
        assert!(validate_export_settings("vertical", "crop", 0.5, 0.5, 40).is_err());
        assert_ne!(new_batch_id(), new_export_id());
    }

    #[test]
    fn cloud_assessments_merge_without_overwriting_local_evidence() {
        use super::storage::Database;
        use serde_json::json;
        use std::fs;

        let directory = std::env::temp_dir().join(format!("bread-storage-test-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("test.sqlite3");
        let _ = fs::remove_file(&path);

        let mut database = Database::open(&path).unwrap();
        let source_id = database.register_source(&super::MediaInfo {
            path: "C:/videos/gameplay.mp4".into(),
            name: "gameplay.mp4".into(),
            duration_seconds: 696.7,
            size_bytes: 1_000,
            width: Some(1280),
            height: Some(720),
            frame_rate: Some("60.00 fps".into()),
            video_codec: Some("h264".into()),
            audio_codec: Some("aac".into()),
            audio_tracks: 1,
        }).unwrap();
        database.create_job("job-1", source_id, "{}").unwrap();
        database.update_event(&json!({
            "job_id": "job-1", "type": "candidate", "candidate": {
                "start_ms": 18_000, "peak_ms": 35_000, "end_ms": 36_733,
                "score": 31, "signals": ["visual_activity"],
                "evidence": [], "explanation": "sampled frames showed a visual scene change",
                "title": "Visual Moment 00-35", "title_source": "evidence_fallback"
            }
        })).unwrap();

        database.apply_ai_item("job-1", &json!({
            "candidateId": 1, "windowStartMs": 25_000, "windowEndMs": 36_733,
            "origin": "candidate", "isHighlight": true,
            "eventType": "goal", "interestScore": 91, "peakMs": 35_200,
            "reason": "A goal is scored after a strong offensive play.",
            "aiTitle": "Goal - A goal is scored after a strong offensive play.",
            "focusX": 0.52, "focusY": 0.48,
            "combinedScore": 73, "model": "gemini-3.8-flash"
        })).unwrap();

        let candidates = database.candidates("job-1").unwrap();
        assert_eq!(candidates.len(), 1, "AI evidence must not create a duplicate row");
        let merged = &candidates[0];
        assert_eq!(merged.score, 31, "the local score is preserved");
        assert_eq!(merged.signals, vec!["visual_activity".to_string()]);
        assert_eq!(merged.ai_score, Some(91));
        assert_eq!(merged.ai_event_type.as_deref(), Some("goal"));
        assert_eq!(merged.combined_score, Some(73));
        assert_eq!(merged.ai_peak_ms, Some(35_200));
        assert!((merged.ai_focus_x.unwrap() - 0.52).abs() < 0.001);

        // A sampled window that Gemini found a highlight in becomes a new candidate.
        database.apply_ai_item("job-1", &json!({
            "candidateId": null, "windowStartMs": 100_000, "windowEndMs": 120_000,
            "origin": "sample", "isHighlight": true,
            "eventType": "funny_moment", "interestScore": 74, "peakMs": 110_000,
            "reason": "A glitch throws the player across the map.",
            "aiTitle": "Funny moment - A glitch throws the player across the map.",
            "focusX": null, "focusY": null,
            "combinedScore": 30, "model": "gemini-3.8-flash"
        })).unwrap();
        let after = database.candidates("job-1").unwrap();
        assert_eq!(after.len(), 2);
        let added = after.iter().find(|item| item.origin == "gemini").expect("AI candidate");
        assert_eq!(added.peak_ms, 110_000);
        assert_eq!(added.ai_event_type.as_deref(), Some("funny_moment"));

        database.set_job_ai_status("job-1", "rate_limited", Some("Gemini rate limit reached"),
                                   Some("gemini-3.8-flash"), Some(3)).unwrap();
        let entry = database.history().unwrap().into_iter().next().unwrap();
        assert_eq!(entry.ai_status, "rate_limited");
        assert_eq!(entry.ai_windows, 3);
        assert!(entry.ai_error.is_some());

        drop(database);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn clip_ranges_are_never_silently_truncated() {
        assert!(validate_clip_range(0, 1_000, 10_000).is_ok());
        assert!(validate_clip_range(0, 500, 10_000).is_err());
        assert!(validate_clip_range(0, 181_000, 200_000).is_err());
        assert!(validate_clip_range(9_000, 10_001, 10_000).is_err());
        assert!(validate_clip_range(10_000, 9_000, 10_000).is_err());
        assert!(export_is_terminal("completed"));
        assert!(export_is_terminal("failed"));
        assert!(export_is_terminal("cancelled"));
        assert!(!export_is_terminal("rendering"));
    }
}

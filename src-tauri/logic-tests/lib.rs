//! Runs the desktop crate's Tauri-free logic tests without linking Tauri.
//!
//! The Windows GNU toolchain on the development machine cannot produce a
//! loadable test harness for Tauri-linked binaries (STATUS_ENTRYPOINT_NOT_FOUND),
//! while the desktop binary itself links and runs. This crate compiles the same
//! `src/logic.rs` source file so the pure functions stay covered by `cargo test`.

#[path = "../src/logic.rs"]
mod logic;

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

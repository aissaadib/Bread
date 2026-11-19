//! Shared plumbing for the JSONL worker processes (analysis, export, cloud).
//!
//! All three workers speak the same protocol: one JSON object per line on
//! stdout, human-readable diagnostics on stderr. Keeping the readers here means
//! the size limits, decoding and diagnostic capture cannot drift apart.

use std::io::{BufRead, BufReader, Read};
use std::sync::mpsc;
use std::thread::{self, JoinHandle};

/// Largest single worker message accepted (1 MiB).
pub const MAX_MESSAGE_BYTES: usize = 1_048_576;
/// How much worker stderr is kept for error messages.
const MAX_DIAGNOSTIC_BYTES: usize = 16_384;

/// Forward newline-delimited worker output to `sender`, rejecting oversized lines.
pub fn spawn_line_reader(stdout: impl Read + Send + 'static,
                         sender: mpsc::SyncSender<Result<Vec<u8>, String>>,
                         worker: &'static str) -> JoinHandle<()> {
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
                    let _ = sender.send(Err(format!("The {worker} message exceeded the size limit.")));
                    break;
                }
                Err(error) => {
                    let _ = sender.send(Err(format!("Could not read {worker} output: {error}")));
                    break;
                }
            }
        }
    })
}

/// Capture a bounded amount of worker stderr for error messages.
pub fn spawn_stderr_reader(stderr: impl Read + Send + 'static) -> JoinHandle<String> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let mut reader = BufReader::new(stderr);
        let mut chunk = [0_u8; 4096];
        loop {
            match reader.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(length) if bytes.len() < MAX_DIAGNOSTIC_BYTES => {
                    let retained = length.min(MAX_DIAGNOSTIC_BYTES - bytes.len());
                    bytes.extend_from_slice(&chunk[..retained]);
                }
                Ok(_) => {}
            }
        }
        String::from_utf8_lossy(&bytes).into_owned()
    })
}

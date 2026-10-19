//! Enregistrement : le flux est écrit tel quel en `.ts`, puis remis en `.mp4` par ffmpeg (sans réencodage)
//! à l'arrêt ; le `.ts` est supprimé si la conversion réussit.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::RecvTimeoutError;
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::hub::Hub;

pub struct Recording {
    pub path: PathBuf,
    pub started: Instant,
    bytes: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
    thread: JoinHandle<Result<PathBuf, String>>,
}

impl Recording {
    pub fn start(hub: &Hub, dir: &Path) -> std::io::Result<Recording> {
        std::fs::create_dir_all(dir)?;
        let path = dir.join(format!("gl310-{}.ts", chrono::Local::now().format("%Y%m%d-%H%M%S")));
        let mut file = BufWriter::new(File::create(&path)?);
        let (bytes, stop) = (Arc::new(AtomicU64::new(0)), Arc::new(AtomicBool::new(false)));
        let rx = hub.subscribe(4096);
        let (ts, counter, stopping) = (path.clone(), bytes.clone(), stop.clone());
        let thread = thread::Builder::new().name("recorder".into()).spawn(move || {
            while !stopping.load(Ordering::Relaxed) {
                match rx.recv_timeout(Duration::from_millis(200)) {
                    Ok(chunk) => {
                        file.write_all(&chunk).map_err(|e| format!("écriture de {} : {e}", ts.display()))?;
                        counter.fetch_add(chunk.len() as u64, Ordering::Relaxed);
                    }
                    Err(RecvTimeoutError::Timeout) => {}
                    Err(RecvTimeoutError::Disconnected) => return Err("flux perdu pendant l'enregistrement (disque trop lent ?)".into()),
                }
            }
            file.flush().map_err(|e| e.to_string())?;
            drop(file);
            remux(&ts)
        })?;
        Ok(Recording { path, started: Instant::now(), bytes, stop, thread })
    }

    pub fn bytes(&self) -> u64 {
        self.bytes.load(Ordering::Relaxed)
    }

    /// Arrête l'écriture ; la conversion en MP4 continue en arrière-plan (voir `JoinHandle::is_finished`).
    pub fn stop(self) -> JoinHandle<Result<PathBuf, String>> {
        self.stop.store(true, Ordering::Relaxed);
        self.thread
    }
}

fn remux(ts: &Path) -> Result<PathBuf, String> {
    let mp4 = ts.with_extension("mp4");
    let status = Command::new("ffmpeg")
        .args(["-v", "error", "-y", "-i"])
        .arg(ts)
        .args(["-c", "copy", "-movflags", "+faststart"])
        .arg(&mp4)
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|e| format!("ffmpeg introuvable : {e}"))?;
    if !status.success() {
        return Err(format!("conversion en MP4 impossible, le flux brut reste dans {}", ts.display()));
    }
    std::fs::remove_file(ts).map_err(|e| e.to_string())?;
    Ok(mp4)
}

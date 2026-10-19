//! Sorties décodées. ffmpeg décode le flux du boîtier :
//! - l'image (I420 1080p) va dans l'aperçu et dans la caméra virtuelle PipeWire « GL310 », publiée par
//!   `gst-launch-1.0 … pipewiresink mode=provide` (OBS : « Capture de périphérique vidéo (PipeWire) ») ;
//! - le son du jeu (PCM 48 kHz stéréo) va dans l'entrée audio virtuelle « GL310-Audio » (OBS : « Capture d'entrée
//!   audio ») : un module PipeWire-Pulse temporaire (`pactl load-module module-null-sink media.class=Audio/Source/Virtual`)
//!   que `pw-cat` alimente par un lien explicite (`pw-link`) ; pw-cat garde une réserve, donc pas de trous dans le son.
//!
//! Chaque chaîne se relance seule si elle s'arrête. L'entrée audio virtuelle est retirée à la fermeture.

use std::io::{Read, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::hub::Hub;
use crate::journal::Journal;
use crate::picture::{i420_len, i420_to_rgba_half};

pub const WIDTH: usize = 1920;
pub const HEIGHT: usize = 1080;
pub const PREVIEW_SIZE: [usize; 2] = [WIDTH / 2, HEIGHT / 2];
const AUDIO_BLOCK: usize = 48_000 / 40 * 4; // 25 ms de S16LE stéréo
const AUDIO_SOURCE: &str = "gl310_audio"; // entrée audio virtuelle vue par OBS (« GL310-Audio »)
const AUDIO_FEED: &str = "gl310-feed"; // flux pw-cat qui l'alimente

const DECODE_INPUT: [&str; 14] = [
    "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
    "-probesize", "500000", "-analyzeduration", "500000", "-f", "mpegts", "-i",
];

/// Dernière image de l'aperçu (RGBA, demi-résolution) et son numéro.
#[derive(Default)]
pub struct Preview {
    pub rgba: Option<Arc<Vec<u8>>>,
    pub number: u64,
}

pub struct Outputs {
    pub preview: Arc<Mutex<Preview>>,
    audio_peak: Arc<AtomicU32>,
    _audio_source: VirtualAudioSource,
}

impl Outputs {
    pub fn start(hub: Arc<Hub>, journal: Arc<Journal>) -> Outputs {
        let audio_source = VirtualAudioSource::create(&journal);
        let preview = Arc::new(Mutex::new(Preview::default()));
        let audio_peak = Arc::new(AtomicU32::new(0));
        let (h, j, p) = (hub.clone(), journal.clone(), preview.clone());
        thread::Builder::new()
            .name("video".into())
            .spawn(move || keep_running(&j, "image", || run_video(&h, &p, &j)))
            .expect("fil vidéo");
        let peak = audio_peak.clone();
        thread::Builder::new()
            .name("audio".into())
            .spawn(move || keep_running(&journal, "son", || run_audio(&hub, &peak, &journal)))
            .expect("fil audio");
        Outputs { preview, audio_peak, _audio_source: audio_source }
    }

    /// Niveau crête du son (0 à 1) sur les 25 dernières millisecondes.
    pub fn audio_level(&self) -> f32 {
        f32::from_bits(self.audio_peak.load(Ordering::Relaxed))
    }
}

/// Entrée audio virtuelle « GL310-Audio » : module PipeWire-Pulse retiré quand l'appli se ferme.
struct VirtualAudioSource {
    module: Option<String>,
}

impl VirtualAudioSource {
    fn create(journal: &Journal) -> VirtualAudioSource {
        unload_leftovers();
        let output = Command::new("pactl")
            .args(["load-module", "module-null-sink", &format!("sink_name={AUDIO_SOURCE}")])
            .args(["media.class=Audio/Source/Virtual", "channel_map=front-left,front-right"])
            .arg("sink_properties=device.description=GL310-Audio")
            .output();
        match output {
            Ok(o) if o.status.success() => {
                VirtualAudioSource { module: Some(String::from_utf8_lossy(&o.stdout).trim().to_string()) }
            }
            Ok(o) => {
                journal.push(format!("entrée audio GL310-Audio non créée : {}", String::from_utf8_lossy(&o.stderr).trim()));
                VirtualAudioSource { module: None }
            }
            Err(e) => {
                journal.push(format!("entrée audio GL310-Audio non créée (pactl introuvable : {e})"));
                VirtualAudioSource { module: None }
            }
        }
    }
}

impl Drop for VirtualAudioSource {
    fn drop(&mut self) {
        if let Some(module) = &self.module {
            let _ = Command::new("pactl").args(["unload-module", module]).status();
        }
    }
}

/// Retire une entrée GL310-Audio laissée par une appli fermée brutalement.
fn unload_leftovers() {
    let Ok(output) = Command::new("pactl").args(["list", "short", "modules"]).output() else { return };
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        if (line.contains(&format!("sink_name={AUDIO_SOURCE} ")) || line.ends_with(&format!("sink_name={AUDIO_SOURCE}")))
            && let Some(id) = line.split_whitespace().next() {
                let _ = Command::new("pactl").args(["unload-module", id]).status();
            }
    }
}

/// Relie le flux pw-cat à l'entrée audio virtuelle (déjà relié compte comme réussi).
fn link_audio_feed() -> bool {
    ["FL", "FR"].iter().all(|ch| {
        Command::new("pw-link")
            .arg(format!("{AUDIO_FEED}:output_{ch}"))
            .arg(format!("{AUDIO_SOURCE}:input_{ch}"))
            .output()
            .is_ok_and(|o| o.status.success() || String::from_utf8_lossy(&o.stderr).contains("File exists"))
    })
}

fn keep_running(journal: &Journal, what: &str, mut run: impl FnMut() -> Result<(), String>) {
    loop {
        if let Err(e) = run() {
            journal.push(format!("{what} : {e} — relance dans 2 s"));
        }
        thread::sleep(Duration::from_secs(2));
    }
}

/// Un décodeur ffmpeg qui lit le flux du hub sur son entrée et écrit `output` sur sa sortie.
fn decoder(hub: &Hub, output: &[&str]) -> Result<(Child, impl Read), String> {
    let mut child = Command::new("ffmpeg")
        .args(DECODE_INPUT)
        .arg("pipe:0")
        .args(output)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("ffmpeg introuvable ({e})"))?;
    let mut stdin = child.stdin.take().expect("entrée ffmpeg");
    let stdout = child.stdout.take().expect("sortie ffmpeg");
    let rx = hub.subscribe(1024);
    thread::spawn(move || {
        for chunk in rx {
            if stdin.write_all(&chunk).is_err() {
                break;
            }
        }
    });
    Ok((child, stdout))
}

/// Un nœud PipeWire qui publie ce qu'on écrit sur son entrée (format décrit par `parse`).
fn pipewire_node(parse: &[&str], properties: &str) -> Result<(Child, ChildStdin), String> {
    let mut child = Command::new("gst-launch-1.0")
        .args(["-q", "fdsrc", "fd=0", "!"])
        .args(parse)
        .args(["!", "queue", "max-size-buffers=4", "leaky=downstream", "!", "pipewiresink", "mode=provide", "sync=false"])
        .arg(format!("stream-properties=props,{properties}"))
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("gst-launch-1.0 introuvable ({e})"))?;
    let stdin = child.stdin.take().expect("entrée gst");
    Ok((child, stdin))
}

fn stop(children: &mut [&mut Child]) {
    for child in children {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn run_video(hub: &Hub, preview: &Mutex<Preview>, journal: &Journal) -> Result<(), String> {
    let (mut ffmpeg, mut frames) = decoder(hub, &[
        "-map", "0:v:0", "-vf", "scale=1920:1080", "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
    ])?;
    let (mut camera, camera_in) = pipewire_node(
        &["rawvideoparse", "format=i420", "width=1920", "height=1080", "framerate=30/1"],
        "media.class=Video/Source,media.role=Camera,node.name=gl310-camera,node.description=GL310",
    )?;
    let mut camera_in = Some(camera_in);
    let mut frame = vec![0u8; i420_len(WIDTH, HEIGHT)];
    let result = loop {
        if let Err(e) = frames.read_exact(&mut frame) {
            break Err(format!("décodeur vidéo arrêté ({e})"));
        }
        if let Some(input) = camera_in.as_mut()
            && input.write_all(&frame).is_err() {
                journal.push("caméra PipeWire GL310 arrêtée : l'aperçu continue sans elle");
                camera_in = None;
            }
        let rgba = Arc::new(i420_to_rgba_half(&frame, WIDTH, HEIGHT));
        let mut p = preview.lock().unwrap();
        p.rgba = Some(rgba);
        p.number += 1;
    };
    drop(camera_in);
    stop(&mut [&mut ffmpeg, &mut camera]);
    result
}

fn run_audio(hub: &Hub, peak: &AtomicU32, journal: &Journal) -> Result<(), String> {
    let (mut ffmpeg, mut pcm) = decoder(hub, &["-map", "0:a:0", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"])?;
    let mut feed = Command::new("pw-cat")
        .args(["--playback", "--target", "0", "-P"])
        .arg(format!("{{ node.autoconnect = false, node.name = {AUDIO_FEED}, node.description = GL310-son-du-jeu }}"))
        .args(["--format", "s16", "--rate", "48000", "--channels", "2", "--latency", "100ms", "-"])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("pw-cat introuvable ({e})"))?;
    let mut feed_in = feed.stdin.take();
    let (mut linked, mut last_try) = (false, Instant::now() - Duration::from_secs(1));
    let mut block = vec![0u8; AUDIO_BLOCK];
    let result = loop {
        if let Err(e) = pcm.read_exact(&mut block) {
            break Err(format!("décodeur audio arrêté ({e})"));
        }
        let max = block.as_chunks::<2>().0.iter().map(|s| i16::from_le_bytes(*s).unsigned_abs()).max().unwrap_or(0);
        peak.store((max as f32 / 32768.0).to_bits(), Ordering::Relaxed);
        if let Some(input) = feed_in.as_mut()
            && input.write_all(&block).is_err()
        {
            journal.push("son du jeu : pw-cat arrêté, l'entrée GL310-Audio reste muette");
            feed_in = None;
        }
        if !linked && last_try.elapsed() >= Duration::from_millis(200) {
            linked = link_audio_feed();
            last_try = Instant::now();
            if linked {
                journal.push("son du jeu relié à l'entrée audio GL310-Audio");
            }
        }
    };
    drop(feed_in);
    stop(&mut [&mut ffmpeg, &mut feed]);
    result
}

//! `Engine` : le seul fil qui possède l'USB. Il ouvre le boîtier dès qu'il est branché, le démarre
//! (firmwares), lance l'encodeur, applique les réglages et pousse le flux dans le hub.
//! L'interface lui parle par des `Command` et lit son état (`BoxStatus`).

use std::path::PathBuf;
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender, TryRecvError};
use std::sync::{Arc, Mutex};
use std::thread::{self, sleep, JoinHandle};
use std::time::{Duration, Instant};

use crate::device::{Error, Gl310};
use crate::hub::Hub;
use crate::journal::Journal;
use crate::model::{BoxStatus, Settings, Signal};

pub enum Command {
    Start,
    Stop,
    Apply(Settings),
    Quit,
}

pub struct Engine {
    tx: Sender<Command>,
    status: Arc<Mutex<BoxStatus>>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl Engine {
    pub fn spawn(hub: Arc<Hub>, journal: Arc<Journal>, settings: Settings, firmware_dir: PathBuf) -> Engine {
        let (tx, rx) = channel();
        let status = Arc::new(Mutex::new(BoxStatus::Absent));
        let worker = Worker { hub, journal, status: status.clone(), settings, firmware_dir, want_stream: true };
        let thread = thread::Builder::new().name("usb".into()).spawn(move || worker.run(rx)).expect("fil USB");
        Engine { tx, status, thread: Mutex::new(Some(thread)) }
    }

    pub fn send(&self, command: Command) {
        let _ = self.tx.send(command);
    }

    pub fn status(&self) -> BoxStatus {
        self.status.lock().unwrap().clone()
    }

    /// Arrête l'encodeur et rend l'USB proprement (à la fermeture de l'appli).
    pub fn quit(&self) {
        self.send(Command::Quit);
        if let Some(thread) = self.thread.lock().unwrap().take() {
            let _ = thread.join();
        }
    }
}

struct Worker {
    hub: Arc<Hub>,
    journal: Arc<Journal>,
    status: Arc<Mutex<BoxStatus>>,
    settings: Settings,
    firmware_dir: PathBuf,
    want_stream: bool,
}

impl Worker {
    fn set(&self, status: BoxStatus) {
        *self.status.lock().unwrap() = status;
    }

    fn run(mut self, rx: Receiver<Command>) {
        // Après un démarrage raté, rien ne sert de réessayer : il faut débrancher le boîtier.
        let mut needs_replug = false;
        loop {
            let pause = match Gl310::open() {
                Ok(Some(_)) if needs_replug => Duration::from_secs(1),
                Ok(Some(dev)) => match self.session(&dev, &rx) {
                    Ok(()) => return,
                    Err(SessionError::Boot(e)) => {
                        self.journal.push(format!("démarrage du boîtier impossible : {e}"));
                        self.set(BoxStatus::Failed { error: format!("{e} — débranche le boîtier 10 s puis rebranche-le") });
                        needs_replug = true;
                        Duration::from_secs(1)
                    }
                    Err(SessionError::Usb(e)) => {
                        self.journal.push(format!("liaison USB perdue : {e}"));
                        Duration::from_secs(1)
                    }
                },
                Ok(None) => {
                    if !matches!(self.status.lock().unwrap().clone(), BoxStatus::Absent) {
                        self.journal.push("boîtier débranché");
                    }
                    needs_replug = false;
                    self.set(BoxStatus::Absent);
                    Duration::from_secs(1)
                }
                Err(e) => {
                    self.set(BoxStatus::Failed { error: e.to_string() });
                    Duration::from_secs(2)
                }
            };
            if self.wait_commands(&rx, pause) {
                return;
            }
        }
    }

    /// Attend `pause` en traitant les commandes (hors connexion) ; renvoie vrai pour quitter.
    fn wait_commands(&mut self, rx: &Receiver<Command>, pause: Duration) -> bool {
        let end = Instant::now() + pause;
        loop {
            let left = end.saturating_duration_since(Instant::now());
            match rx.recv_timeout(left) {
                Ok(Command::Quit) | Err(RecvTimeoutError::Disconnected) => return true,
                Ok(Command::Start) => self.want_stream = true,
                Ok(Command::Stop) => self.want_stream = false,
                Ok(Command::Apply(s)) => self.settings = s,
                Err(RecvTimeoutError::Timeout) => return false,
            }
        }
    }

    /// Pilote le boîtier jusqu'à la commande Quit (`Ok`) ou une erreur.
    fn session(&mut self, dev: &Gl310, rx: &Receiver<Command>) -> Result<(), SessionError> {
        if dev.firmware_loaded()? {
            self.journal.push("boîtier trouvé, firmware déjà en RAM : remise à zéro de l'encodeur");
            dev.stop_encoder()?;
        } else {
            self.journal.push("boîtier trouvé : chargement des firmwares");
            let (status, journal) = (self.status.clone(), self.journal.clone());
            dev.boot(&self.firmware_dir, &mut |step| {
                journal.push(format!("démarrage : {step}"));
                *status.lock().unwrap() = BoxStatus::Booting { step: step.to_string() };
            })
            .map_err(SessionError::Boot)?;
            self.journal.push("firmwares chargés, boîtier prêt");
        }

        let mut streaming = false;
        let mut signal = dev.signal()?;
        let mut last_check = Instant::now();
        let (mut bytes, mut messages) = (0usize, 0u32);
        self.journal.push(describe_signal(signal));
        loop {
            loop {
                match rx.try_recv() {
                    Ok(Command::Start) => self.want_stream = true,
                    Ok(Command::Stop) => self.want_stream = false,
                    Ok(Command::Apply(new)) => self.apply(dev, new, streaming)?,
                    Ok(Command::Quit) | Err(TryRecvError::Disconnected) => {
                        if streaming {
                            dev.stop_encoder()?;
                            self.journal.push("encodeur arrêté");
                        }
                        return Ok(());
                    }
                    Err(TryRecvError::Empty) => break,
                }
            }
            if self.want_stream != streaming {
                if self.want_stream {
                    self.hub.new_session(); // les clients réseau se reconnecteront sur le nouveau flux
                    dev.start_encoder(&self.settings)?;
                    self.journal.push(format!("encodeur démarré à {} Mbit/s", self.settings.video_kbps / 1000));
                } else {
                    dev.stop_encoder()?;
                    self.journal.push("encodeur arrêté");
                }
                streaming = self.want_stream;
            }

            if last_check.elapsed() >= Duration::from_secs(1) {
                let elapsed = last_check.elapsed().as_secs_f32();
                let now = dev.signal()?;
                if now != signal {
                    self.journal.push(describe_signal(now));
                    // Console réveillée ou résolution changée : l'encodeur repart sur le nouveau signal.
                    if streaming && now.is_some() {
                        dev.stop_encoder()?;
                        self.hub.new_session(); // les clients réseau se reconnecteront sur le nouveau flux
                        dev.start_encoder(&self.settings)?;
                        self.journal.push("encodeur relancé sur le nouveau signal");
                    }
                    signal = now;
                }
                self.set(if streaming {
                    BoxStatus::Streaming {
                        signal,
                        mbps: bytes as f32 * 8.0 / 1e6 / elapsed,
                        messages_per_s: messages as f32 / elapsed,
                    }
                } else {
                    BoxStatus::Idle { signal }
                });
                (bytes, messages, last_check) = (0, 0, Instant::now());
            }

            if !streaming {
                sleep(Duration::from_millis(50));
                continue;
            }
            match dev.poll_ts()? {
                Some(chunk) => {
                    bytes += chunk.len();
                    messages += 1;
                    self.hub.feed(&chunk);
                }
                None => sleep(Duration::from_millis(2)),
            }
        }
    }

    fn apply(&mut self, dev: &Gl310, new: Settings, streaming: bool) -> Result<(), Error> {
        let old = std::mem::replace(&mut self.settings, new);
        if !streaming || old == new {
            return Ok(());
        }
        dev.stop_encoder()?;
        self.hub.new_session(); // les clients réseau se reconnecteront sur le nouveau flux
        dev.start_encoder(&new)?;
        self.journal.push(format!(
            "encodeur relancé : {} Mbit/s, images clés toutes les {} images, AAC {} kbit/s, AUDIO OUT {}",
            new.video_kbps / 1000, new.keyframe_interval, new.audio_kbps, if new.audio_out { "oui" } else { "non" }
        ));
        Ok(())
    }
}

fn describe_signal(signal: Option<Signal>) -> String {
    match signal {
        Some(s) => format!("signal HDMI : {}×{} à {} Hz", s.width, s.height, s.refresh_hz),
        None => "pas de signal HDMI (console éteinte ou en veille ?)".into(),
    }
}

enum SessionError {
    Boot(Error),
    Usb(Error),
}

impl From<Error> for SessionError {
    fn from(e: Error) -> Self {
        SessionError::Usb(e)
    }
}

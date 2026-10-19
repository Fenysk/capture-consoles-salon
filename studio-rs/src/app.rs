//! Fenêtre de GL310 Studio : aperçu au centre, état en haut, commandes à droite
//! (flux, enregistrement, réglages, sorties pour OBS et la tour, journal).

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use eframe::egui::{self, Color32, ColorImage, RichText, TextureHandle, TextureOptions};

use crate::engine::{Command, Engine};
use crate::hub::Hub;
use crate::journal::Journal;
use crate::model::{BoxStatus, Settings, Signal, AUDIO_KBPS_CHOICES, KEYFRAME_CHOICES, VIDEO_KBPS_CHOICES};
use crate::outputs::{Outputs, PREVIEW_SIZE};
use crate::recorder::Recording;

pub struct Paths {
    pub settings: PathBuf,
    pub recordings: PathBuf,
}

/// Adresses du flux sur le réseau local, affichées pour OBS sur la tour.
pub struct StreamUrls {
    pub srt: String,
    pub http: String,
}

pub struct StudioApp {
    engine: Arc<Engine>,
    hub: Arc<Hub>,
    journal: Arc<Journal>,
    outputs: Outputs,
    paths: Paths,
    urls: StreamUrls,
    applied: Settings,
    draft: Settings,
    encoder_wanted: bool,
    texture: Option<TextureHandle>,
    shown_frame: u64,
    last_frame_at: Option<Instant>,
    recording: Option<Recording>,
    finishing: Vec<JoinHandle<Result<PathBuf, String>>>,
}

impl StudioApp {
    pub fn new(
        engine: Arc<Engine>,
        hub: Arc<Hub>,
        journal: Arc<Journal>,
        outputs: Outputs,
        paths: Paths,
        settings: Settings,
        urls: StreamUrls,
    ) -> Self {
        StudioApp {
            engine, hub, journal, outputs, paths, urls,
            applied: settings, draft: settings, encoder_wanted: true,
            texture: None, shown_frame: 0, last_frame_at: None,
            recording: None, finishing: Vec::new(),
        }
    }

    fn apply(&mut self, settings: Settings) {
        match settings.validate() {
            Ok(s) => {
                self.applied = s;
                self.engine.send(Command::Apply(s));
                if let Err(e) = std::fs::write(&self.paths.settings, s.to_text()) {
                    self.journal.push(format!("réglages non sauvegardés ({}) : {e}", self.paths.settings.display()));
                }
            }
            Err(e) => self.journal.push(format!("réglages refusés : {e}")),
        }
    }

    fn update_preview(&mut self, ctx: &egui::Context) {
        let (rgba, number) = {
            let p = self.outputs.preview.lock().unwrap();
            (p.rgba.clone(), p.number)
        };
        let Some(rgba) = rgba.filter(|_| number != self.shown_frame) else { return };
        let image = ColorImage::from_rgba_unmultiplied(PREVIEW_SIZE, &rgba);
        match &mut self.texture {
            Some(texture) => texture.set(image, TextureOptions::LINEAR),
            None => self.texture = Some(ctx.load_texture("apercu", image, TextureOptions::LINEAR)),
        }
        self.shown_frame = number;
        self.last_frame_at = Some(Instant::now());
    }

    fn collect_finished_recordings(&mut self) {
        let (done, pending): (Vec<_>, Vec<_>) = self.finishing.drain(..).partition(|h| h.is_finished());
        self.finishing = pending;
        for handle in done {
            match handle.join() {
                Ok(Ok(path)) => self.journal.push(format!("enregistrement prêt : {}", shown(&path))),
                Ok(Err(e)) => self.journal.push(format!("enregistrement : {e}")),
                Err(_) => self.journal.push("enregistrement : erreur interne"),
            }
        }
    }

    fn encoder_section(&mut self, ui: &mut egui::Ui) {
        ui.heading("Encodeur");
        let label = if self.encoder_wanted { "Arrêter l'encodeur" } else { "Démarrer l'encodeur" };
        if ui.button(label).clicked() {
            self.encoder_wanted = !self.encoder_wanted;
            self.engine.send(if self.encoder_wanted { Command::Start } else { Command::Stop });
        }
    }

    fn recording_section(&mut self, ui: &mut egui::Ui, streaming: bool) {
        ui.heading("Enregistrement");
        if let Some(rec) = &self.recording {
            let seconds = rec.started.elapsed().as_secs();
            ui.label(RichText::new(format!(
                "● {:02}:{:02}  —  {:.1} Mo", seconds / 60, seconds % 60, rec.bytes() as f64 / 1e6
            )).color(Color32::from_rgb(230, 80, 80)));
            if ui.button("Arrêter l'enregistrement").clicked() {
                let rec = self.recording.take().expect("enregistrement en cours");
                self.journal.push(format!("enregistrement arrêté, conversion en MP4 : {}", shown(&rec.path)));
                self.finishing.push(rec.stop());
            }
        } else if ui.add_enabled(streaming, egui::Button::new("Enregistrer")).clicked() {
            match Recording::start(&self.hub, &self.paths.recordings) {
                Ok(rec) => {
                    self.journal.push(format!("enregistrement : {}", shown(&rec.path)));
                    self.recording = Some(rec);
                }
                Err(e) => self.journal.push(format!("enregistrement impossible : {e}")),
            }
        }
        if !self.finishing.is_empty() {
            ui.label("Conversion en MP4…");
        }
    }

    fn settings_section(&mut self, ui: &mut egui::Ui) {
        ui.heading("Réglages");
        let d = &mut self.draft;
        egui::ComboBox::from_label("Débit vidéo")
            .selected_text(format!("{} Mbit/s", d.video_kbps / 1000))
            .show_ui(ui, |ui| {
                for v in VIDEO_KBPS_CHOICES {
                    ui.selectable_value(&mut d.video_kbps, v, format!("{} Mbit/s", v / 1000));
                }
            });
        egui::ComboBox::from_label("Images clés")
            .selected_text(format!("toutes les {} s", d.keyframe_interval / 30))
            .show_ui(ui, |ui| {
                for v in KEYFRAME_CHOICES {
                    ui.selectable_value(&mut d.keyframe_interval, v, format!("toutes les {} s", v / 30));
                }
            });
        egui::ComboBox::from_label("Débit audio")
            .selected_text(format!("{} kbit/s", d.audio_kbps))
            .show_ui(ui, |ui| {
                for v in AUDIO_KBPS_CHOICES {
                    ui.selectable_value(&mut d.audio_kbps, v, format!("{v} kbit/s"));
                }
            });
        ui.checkbox(&mut d.audio_out, "Son sur la prise AUDIO OUT");
        if ui.add_enabled(self.applied != self.draft, egui::Button::new("Appliquer")).clicked() {
            self.apply(self.draft);
        }
        ui.label(RichText::new("« Appliquer » relance l'encodeur (1 à 2 s de coupure).").small().weak());
        ui.label(RichText::new("Luminosité, contraste : filtre « Correction colorimétrique » dans OBS.").small().weak());
    }

    fn outputs_section(&mut self, ui: &mut egui::Ui) {
        ui.heading("Sorties");
        ui.label("Caméra PipeWire : GL310");
        ui.label("Son du jeu (entrée audio) : GL310-Audio");
        ui.add(egui::ProgressBar::new(self.outputs.audio_level()).desired_height(6.0));
        ui.label(RichText::new("OBS : « Capture de périphérique vidéo (PipeWire) » et « Capture d'entrée audio »").small().weak());
        ui.add_space(4.0);
        // Adresses et boutons l'un sous l'autre : côte à côte, ils débordent de la colonne.
        for (label, url, button) in [
            ("SRT, latence fixe (OBS « Source média » sur la tour) :", &self.urls.srt, "Copier l'adresse SRT"),
            ("HTTP (secours, app web) :", &self.urls.http, "Copier l'adresse HTTP"),
        ] {
            ui.label(label);
            ui.monospace(url);
            if ui.small_button(button).clicked() {
                ui.ctx().copy_text(url.clone());
            }
        }
    }

    fn journal_section(&self, ui: &mut egui::Ui) {
        ui.heading("Journal");
        egui::ScrollArea::vertical().max_height(220.0).stick_to_bottom(true).show(ui, |ui| {
            for line in self.journal.lines().iter().rev().take(60).rev() {
                ui.label(RichText::new(line).small().monospace());
            }
        });
    }
}

/// `recordings/gl310-….mp4` plutôt que le chemin complet.
fn shown(path: &Path) -> String {
    let name = |p: Option<&Path>| p.and_then(Path::file_name).map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    format!("{}/{}", name(path.parent()), name(Some(path)))
}

fn signal_text(signal: &Option<Signal>) -> String {
    match signal {
        Some(s) => format!("{}×{} à {} Hz", s.width, s.height, s.refresh_hz),
        None => "pas de signal HDMI".into(),
    }
}

fn describe(status: &BoxStatus) -> (String, Color32) {
    match status {
        BoxStatus::Absent => ("Boîtier non branché".into(), Color32::GRAY),
        BoxStatus::Booting { step } => (format!("Démarrage du boîtier : {step}…"), Color32::from_rgb(230, 180, 60)),
        BoxStatus::Idle { signal } => (format!("Prêt, encodeur arrêté — {}", signal_text(signal)), Color32::from_rgb(120, 170, 230)),
        BoxStatus::Streaming { signal, mbps, .. } => {
            (format!("En direct — {} — {mbps:.1} Mbit/s", signal_text(signal)), Color32::from_rgb(90, 200, 120))
        }
        BoxStatus::Failed { error } => (format!("Erreur : {error}"), Color32::from_rgb(230, 90, 90)),
    }
}

impl eframe::App for StudioApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        ctx.request_repaint_after(Duration::from_millis(33));
        self.update_preview(&ctx);
        self.collect_finished_recordings();
        let status = self.engine.status();
        let streaming = matches!(status, BoxStatus::Streaming { .. });

        egui::Panel::top("etat").show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.heading("GL310 Studio");
                ui.add_space(12.0);
                let (text, color) = describe(&status);
                ui.label(RichText::new(text).color(color).strong());
            });
        });
        egui::Panel::right("commandes").resizable(false).exact_size(340.0).show(ui, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                self.encoder_section(ui);
                ui.separator();
                self.recording_section(ui, streaming);
                ui.separator();
                self.settings_section(ui);
                ui.separator();
                self.outputs_section(ui);
                ui.separator();
                self.journal_section(ui);
            });
        });
        egui::CentralPanel::default().show(ui, |ui| {
            let fresh = self.last_frame_at.is_some_and(|t| t.elapsed() < Duration::from_secs(2));
            match (&self.texture, fresh) {
                (Some(texture), true) => {
                    let room = ui.available_size();
                    let size = if room.x / room.y > 16.0 / 9.0 {
                        egui::vec2(room.y * 16.0 / 9.0, room.y)
                    } else {
                        egui::vec2(room.x, room.x * 9.0 / 16.0)
                    };
                    ui.centered_and_justified(|ui| {
                        ui.add(egui::Image::new((texture.id(), size)).fit_to_exact_size(size));
                    });
                }
                _ => {
                    ui.centered_and_justified(|ui| ui.label(RichText::new("Pas d'image").size(22.0).color(Color32::GRAY)));
                }
            }
        });
    }
}

impl Drop for StudioApp {
    /// À la fermeture : terminer l'enregistrement en cours (conversion en MP4 comprise).
    fn drop(&mut self) {
        if let Some(rec) = self.recording.take() {
            self.finishing.push(rec.stop());
        }
        for handle in self.finishing.drain(..) {
            match handle.join() {
                Ok(Ok(path)) => self.journal.push(format!("enregistrement prêt : {}", shown(&path))),
                Ok(Err(e)) => self.journal.push(format!("enregistrement : {e}")),
                Err(_) => self.journal.push("enregistrement : erreur interne"),
            }
        }
    }
}

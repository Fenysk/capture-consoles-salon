//! GL310 Studio : aperçu, enregistrement, réglages et état de l'AVerMedia Live Gamer Portable Lite (GL310).
//!
//! L'appli parle elle-même au boîtier en USB (pas de pilote noyau) et publie :
//!   - une caméra virtuelle PipeWire « GL310 » et le son du jeu en entrée audio « GL310-Audio » (OBS sous Linux) ;
//!   - le flux MPEG-TS en HTTP sur /stream.ts, ouvert au réseau local (OBS « Source média » sur la tour, app web).
//!
//! Usage : gl310-studio [--listen 0.0.0.0:4848]   (--listen 127.0.0.1:4848 pour ce PC seulement)

mod app;
mod device;
mod engine;
mod hub;
mod http;
mod journal;
mod model;
mod outputs;
mod picture;
mod recorder;
mod tables;
mod ts;

use std::net::{IpAddr, UdpSocket};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use eframe::egui;

use crate::app::{Paths, StudioApp};
use crate::engine::Engine;
use crate::hub::Hub;
use crate::journal::Journal;
use crate::model::Settings;
use crate::outputs::Outputs;

const PROJECT_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/..");
const DEFAULT_LISTEN: &str = "0.0.0.0:4848";
const ICON_PNG: &[u8] = include_bytes!("../assets/gl310-studio.png");

fn home() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
}

fn xdg_dir(variable: &str, fallback: &str) -> PathBuf {
    std::env::var_os(variable).map(PathBuf::from).unwrap_or_else(|| home().join(fallback))
}

fn config_file() -> PathBuf {
    xdg_dir("XDG_CONFIG_HOME", ".config").join("gl310-studio/settings.txt")
}

/// Firmwares : la copie faite par install.sh, sinon ceux du dossier du projet.
fn firmware_dir(project: &Path) -> PathBuf {
    let installed = xdg_dir("XDG_DATA_HOME", ".local/share").join("gl310-studio/firmware");
    if installed.join("qpvidfwusb.bin").exists() { installed } else { project.join("vendor/gl310-driver/extracted") }
}

/// Enregistrements dans le dossier Vidéos de l'utilisateur (~/Vidéos/GL310).
fn recordings_dir() -> PathBuf {
    let videos = std::process::Command::new("xdg-user-dir")
        .arg("VIDEOS")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|dir| !dir.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join("Videos"));
    videos.join("GL310")
}

/// Adresse du portable sur le réseau local (celle de la route par défaut ; aucun paquet n'est envoyé).
fn lan_ip() -> Option<IpAddr> {
    let socket = UdpSocket::bind("0.0.0.0:0").ok()?;
    socket.connect("192.0.2.1:80").ok()?;
    Some(socket.local_addr().ok()?.ip())
}

fn load_settings(path: &PathBuf, journal: &Journal) -> Settings {
    match std::fs::read_to_string(path) {
        Ok(text) => Settings::from_text(&text).unwrap_or_else(|e| {
            journal.push(format!("réglages de {} ignorés : {e}", path.display()));
            Settings::default()
        }),
        Err(_) => Settings::default(),
    }
}

fn main() -> eframe::Result {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let listen = match args.as_slice() {
        [] => DEFAULT_LISTEN.to_string(),
        [flag, addr] if flag == "--listen" => addr.clone(),
        _ => {
            eprintln!("Usage : gl310-studio [--listen 0.0.0.0:4848]");
            std::process::exit(2);
        }
    };
    let project = std::fs::canonicalize(PROJECT_DIR).unwrap_or_else(|_| PathBuf::from(PROJECT_DIR));
    let journal = Arc::new(Journal::default());
    let hub = Arc::new(Hub::default());
    let settings_path = config_file();
    if let Some(dir) = settings_path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let settings = load_settings(&settings_path, &journal);

    let host = lan_ip().map_or("127.0.0.1".to_string(), |ip| ip.to_string());
    let stream_url = format!("http://{}/stream.ts", listen.replace("0.0.0.0", &host));
    let log = journal.clone();
    match http::serve(&listen, hub.clone(), move |line| log.push(line)) {
        Ok(()) => journal.push(format!("flux réseau : {stream_url}")),
        Err(e) => journal.push(format!("flux réseau indisponible sur {listen} : {e}")),
    }
    let engine = Arc::new(Engine::spawn(
        hub.clone(),
        journal.clone(),
        settings,
        firmware_dir(&project),
    ));
    let outputs = Outputs::start(hub.clone(), journal.clone());

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("GL310 Studio")
            .with_app_id("gl310-studio")
            .with_icon(eframe::icon_data::from_png_bytes(ICON_PNG).expect("icône PNG intégrée"))
            .with_inner_size([1280.0, 780.0])
            .with_min_inner_size([900.0, 560.0]),
        ..Default::default()
    };
    let paths = Paths { settings: settings_path, recordings: recordings_dir() };
    let app_engine = engine.clone();
    let result = eframe::run_native(
        "GL310 Studio",
        options,
        Box::new(move |_cc| Ok(Box::new(StudioApp::new(app_engine, hub, journal, outputs, paths, settings, stream_url)))),
    );
    engine.quit();
    result
}
